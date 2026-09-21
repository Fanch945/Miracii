from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "chat.db"
DEFAULT_SESSION = "local"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, created_at) VALUES (?, ?)",
            (DEFAULT_SESSION, _now()),
        )
        conn.commit()


def ensure_session(session_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, created_at) VALUES (?, ?)",
            (session_id, _now()),
        )
        conn.commit()


def add_message(session_id: str, role: str, content: str) -> dict:
    ensure_session(session_id)
    created_at = _now()
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, created_at),
        )
        conn.commit()
        return {
            "id": cursor.lastrowid,
            "session_id": session_id,
            "role": role,
            "content": content,
            "created_at": created_at,
        }


def list_messages(session_id: str, limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, role, content, created_at
            FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def recent_dialogue(session_id: str, limit: int = 24) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT role, content
            FROM messages
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def max_message_id(session_id: str) -> int | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(id) AS mid FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    mid = row["mid"] if row else None
    return int(mid) if mid is not None else None


def max_created_at(session_id: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT created_at FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return row["created_at"] if row else None


def delete_message(message_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        conn.commit()


def delete_messages_after(session_id: str, after_id: int | None) -> int:
    """Delete unconsolidated tail. after_id None means delete all in the session."""
    with connect() as conn:
        if after_id is None:
            cursor = conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        else:
            cursor = conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND id > ?",
                (session_id, after_id),
            )
        conn.commit()
        return int(cursor.rowcount or 0)


def count_after(session_id: str, after_id: int | None) -> int:
    with connect() as conn:
        if after_id is None:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id = ? AND id > ?",
                (session_id, after_id),
            ).fetchone()
    return int(row["n"] if row else 0)


def clear_messages(session_id: str) -> None:
    delete_messages_after(session_id, None)
