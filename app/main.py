from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import APP_CHANNEL, APP_VERSION, clock, db, files, media, persona
from .llm import LlmError, MODEL, stream_reply

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
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
    """Older turns stay text. Only the latest user turn may carry image blocks."""
    rows = db.recent_dialogue(sid, limit=24)
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
def list_files() -> dict:
    return {"groups": files.list_readable()}


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
    history = _llm_history(sid, current_images=saved_images)
    scan_bits = []
    for item in db.recent_dialogue(sid, limit=6):
        piece, _rels = media.split_content(item["content"])
        if piece:
            scan_bits.append(piece)
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
