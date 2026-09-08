from __future__ import annotations

import json
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import clock, db, files, persona
from .llm import LlmError, stream_reply

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Miracii", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ChatRequest(BaseModel):
    text: str = Field(..., min_length=1)
    session_id: str | None = Field(default=None, max_length=64)


class PersonaRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)


def _session(session_id: str | None) -> str:
    return (session_id or persona.active_id()).strip() or persona.active_id()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/meta")
def meta() -> dict:
    current = persona.active_id()
    return {
        "name": persona.display_name(current),
        "persona_id": current,
        "main_persona": persona.MAIN_ID,
        "model": "dsapi Zero · deepseek-v4-flash",
        "session_id": current,
        "personas": persona.list_personas(),
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
    sid = _session(session_id)
    return {"session_id": sid, "messages": db.list_messages(sid)}


@app.delete("/api/history")
def clear_history(session_id: str | None = None) -> dict:
    sid = _session(session_id)
    db.clear_messages(sid)
    return {"ok": True}


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
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="空消息")

    sid = _session(body.session_id)
    speaker = persona.display_name(sid)
    clock.on_chat(sid)
    user_msg = db.add_message(sid, "user", text)
    files.append_turn(sid, speaker, "user", text, user_msg["created_at"])
    history = db.recent_dialogue(sid, limit=24)
    scan = "\n".join(item["content"] for item in history[-6:])
    messages = [{"role": "system", "content": persona.system_prompt(sid, scan_text=scan)}, *history]

    async def event_stream():
        yield _sse("user", user_msg)
        collected: list[str] = []
        try:
            async for token in stream_reply(messages):
                collected.append(token)
                yield _sse("token", {"text": token})
        except LlmError as exc:
            yield _sse("error", {"message": str(exc)})
            return

        reply = "".join(collected).strip()
        if not reply:
            yield _sse("error", {"message": "模型没有返回文本"})
            return
        saved = db.add_message(sid, "assistant", reply)
        files.append_turn(sid, speaker, "assistant", reply, saved["created_at"])
        clock.on_chat(sid)
        yield _sse("done", saved)

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
