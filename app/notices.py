"""Short consolidation notices for the chat window. Not the supervisor log."""

from __future__ import annotations

import json
from datetime import datetime

from .files import DATA_DIR

PATH = DATA_DIR / "notices.jsonl"
_LABEL = {
    "stm": "短期记忆",
    "routine": "印象",
    "daily": "当日归档",
    "weekly": "周图式",
}


def push(persona_id: str, phase: str, *, wrote: bool) -> None:
    label = _LABEL.get(phase, phase)
    text = f"{label}已写下" if wrote else f"{label}：没有新内容"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "t": datetime.now().astimezone().isoformat(timespec="seconds"),
        "persona_id": persona_id,
        "phase": phase,
        "text": text,
    }
    with PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def since(after: int, persona_id: str | None = None) -> dict:
    """`after` is a line count. First call with catchup skips history."""
    if not PATH.is_file():
        return {"after": 0, "items": []}
    lines = [ln for ln in PATH.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    total = len(lines)
    start = max(0, min(after, total))
    items = []
    for raw in lines[start:]:
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if persona_id and row.get("persona_id") != persona_id:
            continue
        items.append(row)
    return {"after": total, "items": items}
