from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import APP_CHANNEL, APP_VERSION, clock, db, files, heartbeat, media, notices, persona, starmap, studio, warmth
from .llm import LlmError, MODEL, stream_reply

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    try:
        starmap.sync(persona.active_id())
    except Exception:  # a stale index must not stop the app from serving
        pass
    yield


app = FastAPI(title="Miracii", version=APP_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ChatImage(BaseModel):
    mime: str = Field(..., min_length=3, max_length=32)
    data: str = Field(..., min_length=8)


class ChatRequest(BaseModel):
    text: str = Field(default="", max_length=4000)
    session_id: str | None = Field(default=None, max_length=64)
    images: list[ChatImage] = Field(default_factory=list)


class PersonaRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)


def _session(session_id: str | None) -> str:
    return (session_id or persona.active_id()).strip() or persona.active_id()


def _lock_id(sid: str) -> int | None:
    lock = clock.lock_status(sid)
    lock_id = lock.get("locked_through_id")
    if lock_id is not None:
        return int(lock_id)
    stamp = lock.get("locked_through_at")
    if not stamp:
        return None
    due = None
    try:
        due = datetime.fromisoformat(stamp)
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    last = None
    for message in db.list_messages(sid):
        try:
            created = datetime.fromisoformat(message["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created <= due:
            last = int(message["id"])
    return last


def _history_payload(sid: str) -> dict:
    lock = clock.lock_status(sid)
    lock_id = _lock_id(sid)
    return {
        "session_id": sid,
        "messages": [_public_message(item) for item in db.list_messages(sid)],
        "locked_through_id": lock_id,
        "locked_through_at": lock.get("locked_through_at"),
        "layers": clock.memory_layers(sid),
        "can_reset": db.count_after(sid, lock_id) > 0,
        "unconsolidated_count": db.count_after(sid, lock_id),
    }


def _public_message(item: dict) -> dict:
    text, rels = media.split_content(item.get("content") or "")
    payload = dict(item)
    payload["content"] = text
    payload["images"] = [media.public_url(rel) for rel in rels]
    return payload


def _llm_history(sid: str, current_images: list[dict] | None = None) -> list[dict]:
    """Only the sitting that has not been written to STM. Consolidated lines stay in files."""
    rows = db.recent_dialogue(sid, limit=24, after_id=_lock_id(sid))
    out: list[dict] = []
    last_user = max((i for i, row in enumerate(rows) if row["role"] == "user"), default=-1)
    for index, row in enumerate(rows):
        text, rels = media.split_content(row["content"])
        if row["role"] == "user" and index == last_user and current_images:
            blocks: list[dict] = [{"type": "text", "text": text or "（看）"}]
            for image in current_images:
                blocks.append(
                    {"type": "image_url", "image_url": {"url": image["data_url"], "detail": "auto"}}
                )
            out.append({"role": "user", "content": blocks})
        else:
            if rels and not text:
                text = "（看了一张图）"
            elif rels:
                text = f"{text}\n（附图）"
            out.append({"role": row["role"], "content": text})
    return out


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/studio")
def studio_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "studio.html")


def _judge_turn_async(persona_id: str, user_text: str, reply_text: str) -> None:
    """一轮结束后，让轻量模型把这一轮翻译成六轴增减。

    **后台线程 + 全程吞异常**：她能不能回话不取决于这一次判断；判断失败就当她没被影响。
    """

    def run() -> None:
        try:
            deltas = heartbeat.judge_conversation(persona_id, user_text, reply_text)
            if deltas:
                warmth.apply_conversation(persona_id, deltas, source="chat", note=user_text[:60])
        except Exception:
            pass

    threading.Thread(target=run, name="miracii-warmth-judge", daemon=True).start()


class StudioFile(BaseModel):
    persona: str = Field(..., min_length=1, max_length=64)
    path: str = Field(..., min_length=1, max_length=240)
    content: str | None = None


class StudioCreate(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)


class StudioHold(BaseModel):
    persona: str = Field(..., min_length=1, max_length=64)
    hold: bool = True


def _studio_error(exc: Exception) -> HTTPException:
    status = 404 if isinstance(exc, FileNotFoundError) else 400
    return HTTPException(status_code=status, detail=str(exc))


@app.get("/api/studio/files")
def studio_files(persona_id: str) -> dict:
    try:
        folder = studio.persona_dir(persona_id)
        name = persona.display_name(persona_id) if folder.is_dir() else persona_id
        return {
            "persona": persona_id,
            "name": name,
            "hold": clock.is_held(persona_id),
            "files": studio.list_files(persona_id),
        }
    except (ValueError, FileNotFoundError) as exc:
        raise _studio_error(exc) from exc


@app.get("/api/studio/file")
def studio_read(persona_id: str, path: str) -> dict:
    try:
        return {"persona": persona_id, "path": path, "content": studio.read_file(persona_id, path)}
    except (ValueError, FileNotFoundError) as exc:
        raise _studio_error(exc) from exc


@app.put("/api/studio/file")
def studio_write(body: StudioFile) -> dict:
    if body.content is None:
        raise HTTPException(status_code=400, detail="没有内容")
    try:
        studio.write_file(body.persona, body.path, body.content)
    except (ValueError, FileNotFoundError) as exc:
        raise _studio_error(exc) from exc
    return {"ok": True}


@app.post("/api/studio/file")
def studio_new_file(body: StudioFile) -> dict:
    try:
        studio.create_file(body.persona, body.path)
    except (ValueError, FileNotFoundError) as exc:
        raise _studio_error(exc) from exc
    return {"ok": True, "path": body.path}


@app.post("/api/studio/personas")
def studio_create(body: StudioCreate) -> dict:
    try:
        created = studio.create_persona(body.id)
        clock.set_hold(created, True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "persona": created, "hold": True}


@app.post("/api/studio/hold")
def studio_hold(body: StudioHold) -> dict:
    try:
        studio.persona_dir(body.persona)
        if not studio.persona_dir(body.persona).is_dir():
            raise ValueError("没有这个人格")
        held = clock.set_hold(body.persona, body.hold)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "persona": body.persona, "hold": held}


@app.get("/api/studio/export")
def studio_export(persona_id: str) -> Response:
    try:
        payload = studio.export_zip(persona_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{persona_id}.zip"'},
    )


@app.post("/api/studio/import")
async def studio_import(request: Request, persona_id: str, replace: bool = False) -> dict:
    raw = await request.body()
    try:
        created = studio.import_zip(raw, persona_id, replace=replace)
        clock.set_hold(created, True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "persona": created, "hold": True}


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": APP_VERSION, "channel": APP_CHANNEL}


@app.get("/api/meta")
def meta() -> dict:
    current = persona.active_id()
    return {
        "name": persona.display_name(current),
        "persona_id": current,
        "main_persona": persona.MAIN_ID,
        "model": f"dsapi Zero · {MODEL}",
        "session_id": current,
        "personas": persona.list_personas(),
        "version": APP_VERSION,
        "channel": APP_CHANNEL,
        "hold": clock.is_held(current),
    }


@app.post("/api/persona")
def switch_persona(body: PersonaRequest) -> dict:
    try:
        current = persona.set_active(body.id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "ok": True,
        "persona_id": current,
        "name": persona.display_name(current),
        "session_id": current,
        "personas": persona.list_personas(),
        "hold": clock.is_held(current),
    }


@app.get("/api/history")
def history(session_id: str | None = None) -> dict:
    return _history_payload(_session(session_id))


@app.delete("/api/history")
def clear_history(session_id: str | None = None) -> dict:
    sid = _session(session_id)
    lock = clock.lock_status(sid)
    lock_id = _lock_id(sid)
    pending = db.count_after(sid, lock_id)
    if pending <= 0:
        raise HTTPException(
            status_code=409,
            detail="已经写入短期记忆的对话不能退回。只能撤回尚未巩固的这几句。",
        )
    db.delete_messages_after(sid, lock_id)
    lock_at = lock.get("locked_through_at")
    if lock_at:
        files.trim_transcript_after(sid, lock_at, inclusive=False)
    else:
        files.trim_transcript_after(sid, None)
    clock.after_rewind(sid)
    payload = _history_payload(sid)
    payload["ok"] = True
    payload["removed"] = pending
    return payload


@app.get("/api/inbox")
def inbox(session_id: str | None = None, after_id: int = 0) -> dict:
    sid = _session(session_id)
    messages = [_public_message(item) for item in db.list_messages(sid) if int(item["id"]) > after_id]
    return {"session_id": sid, "messages": messages}


@app.get("/api/media/{persona_id}/{name}")
def media_file(persona_id: str, name: str) -> FileResponse:
    try:
        path = media.resolve(f"{persona_id}/{name}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path)


@app.get("/api/files")
def list_files(persona: str | None = None) -> dict:
    return files.list_readable(persona)


@app.get("/api/runtime")
def runtime_log(persona: str, kind: str = "supervisor") -> dict:
    try:
        return files.runtime_view(persona, kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/warmth")
def warmth_view(persona: str | None = None) -> dict:
    """她此刻的六轴、有效门槛、每道闸门的差距——只读，不改任何状态。

    参数名沿用其他接口的 `persona`；`None` 时由 `warmth.view` 自己取当前人格
    （这里不能引用 persona 模块——参数把它遮住了）。
    """
    try:
        return warmth.view(persona)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/warmth")
def warmth_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "warmth.html")


class WarmthMark(BaseModel):
    text: str = Field("", max_length=400)
    verdict: str = Field("should_speak", max_length=32)
    blame: list[str] = Field(default_factory=list)
    expected: str | None = Field(None, max_length=32)
    tags: list[str] = Field(default_factory=list)


@app.post("/api/warmth/mark")
def warmth_mark(body: WarmthMark, persona: str | None = None) -> dict:
    """就地标注。面板里**唯一**的写入口——只往台账追加，不碰记忆，也不动引擎状态。"""
    try:
        return warmth.mark(
            persona,
            body.text,
            verdict=body.verdict,
            blame=body.blame,
            expected=body.expected,
            tags=body.tags,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/notices")
def list_notices(after: int = 0, persona: str | None = None, catchup: int = 0) -> dict:
    if catchup:
        data = notices.since(10**9, persona)
        data["items"] = []
        return data
    return notices.since(after, persona)


@app.get("/api/files/content")
def file_content(id: str) -> dict:
    try:
        return files.read_readable(id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/chat")
async def chat(body: ChatRequest) -> StreamingResponse:
    text = (body.text or "").strip()
    raw_images = [{"mime": item.mime, "data": item.data} for item in body.images]
    if not text and not raw_images:
        raise HTTPException(status_code=400, detail="空消息")

    sid = _session(body.session_id)
    if clock.is_held(sid):
        raise HTTPException(status_code=409, detail="这位正在编辑设定，回复和巩固已暂停。")
    speaker = persona.display_name(sid)
    try:
        saved_images = media.save_images(sid, raw_images) if raw_images else []
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stored = media.join_content(text or "（看）", [item["rel"] for item in saved_images])
    snap = clock.snapshot()
    clock.on_chat(sid)
    user_msg = db.add_message(sid, "user", stored)
    files.append_turn(sid, speaker, "user", stored.replace("\n", " "), user_msg["created_at"])
    # 这条消息存在 sid 上，积温就该落到同一个人的格子上——
    # 直接写 active_id() 在多个人格之间会回落错人（复核指出）。
    warmth_pid = sid if any(p["id"] == sid for p in persona.list_personas()) else persona.active_id()
    try:
        warmth.on_user_message(warmth_pid)
    except Exception:
        pass  # 积温出问题不该影响聊天本身
    history = _llm_history(sid, current_images=saved_images)
    scan_bits = []
    for item in history:
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            scan_bits.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    scan_bits.append(block["text"])
    system = persona.system_prompt(sid, scan_text="\n".join(scan_bits))
    if saved_images:
        system += "\n\n对方这轮发了图。按你看见的内容说话，不要声称看不见，也不要编造图里没有的东西。"
    messages = [
        {"role": "system", "content": system},
        *history,
    ]
    public_user = _public_message(user_msg)

    def _rollback() -> None:
        db.delete_message(user_msg["id"])
        files.retract_turn(sid, user_msg["created_at"])
        clock.restore(snap)
        try:
            # 这条消息被撤回了，等于"你并没有出现"——积温的回落也要还回去，
            # 否则一次失败的发送会白白解除她的等待（复核指出）。
            warmth.undo_user_message(warmth_pid)
        except Exception:
            pass

    async def event_stream():
        yield _sse("user", public_user)
        collected: list[str] = []
        try:
            async for token in stream_reply(messages):
                collected.append(token)
                yield _sse("token", {"text": token})
        except LlmError as exc:
            _rollback()
            yield _sse("error", {"message": str(exc), "rolled_back": True})
            return

        reply = "".join(collected).strip()
        if not reply:
            _rollback()
            yield _sse("error", {"message": "模型没有返回文本", "rolled_back": True})
            return
        saved = db.add_message(sid, "assistant", reply)
        files.append_turn(sid, speaker, "assistant", reply, saved["created_at"])
        clock.on_chat(sid)
        # 这一轮让她的内心动了多少——后台判，不阻塞这次回复
        _judge_turn_async(warmth_pid, stored, reply)
        yield _sse("done", _public_message(saved))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
