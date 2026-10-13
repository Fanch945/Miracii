"""Retrieve adopted memory into the speaker prompt. Lore stays in lore.py."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from . import persona
from .lore import STORY_INTENT

_KEY_SPLIT = re.compile(r"[,，、;；]")
_KEYS_LINE = re.compile(r"^keys:\s*(.+)$", re.I | re.M)


def _skip(line: str) -> bool:
    s = line.strip()
    return (not s) or s.startswith("#")


def _keys_in(text: str, blob: str) -> list[str]:
    hay = (blob or "").casefold()
    hits: list[str] = []
    for raw in _KEY_SPLIT.split(text or ""):
        key = raw.strip()
        if len(key) >= 2 and key.casefold() in hay:
            hits.append(key)
    return hits


def load_semantic_lines(persona_id: str | None = None) -> list[dict]:
    base = persona.folder(persona_id) / "memory" / "semantic"
    rows: list[dict] = []
    for name in ("people.txt", "facts.txt"):
        path = base / name
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if _skip(raw) or "|" not in raw:
                continue
            bits = [p.strip() for p in raw.split("|")]
            who = bits[0] if bits else ""
            claim = bits[1] if len(bits) > 1 else raw
            keys = bits[3] if len(bits) > 3 else (bits[2] if len(bits) == 3 else "")
            rows.append({"line": raw.strip(), "who": who, "claim": claim, "keys": keys, "file": name})
    return rows


def select_semantic(persona_id: str | None, scan_text: str, *, max_hits: int = 8) -> list[str]:
    rows = load_semantic_lines(persona_id)
    if not rows:
        return []
    text = (scan_text or "").casefold()
    if not text.strip():
        return []
    scored: list[tuple[int, int, str]] = []
    for index, row in enumerate(rows):
        keys_hit = _keys_in(row["keys"], text)
        who_hit = 2 if row["who"] and row["who"].casefold() in text else 0
        score = len(keys_hit) * 3 + who_hit
        if score <= 0:
            continue
        scored.append((score, index, row["line"]))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    seen: set[str] = set()
    out: list[str] = []
    for _, _, line in scored:
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
        if len(out) >= max_hits:
            return out
    return out


def _sitting_blocks(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    chunks = re.split(r"\n(?=# )", text)
    return [c.strip() for c in chunks if c.strip() and not c.strip().startswith("# 格式") and "notes.txt" not in path.name]


def select_sittings(persona_id: str | None, scan_text: str, *, max_sittings: int = 2) -> list[str]:
    """Impression / episodic blocks when the human asks about 'that time' or hits keys."""
    text = (scan_text or "").casefold()
    if not text.strip():
        return []
    intent = any(w.casefold() in text for w in STORY_INTENT)
    base = persona.folder(persona_id) / "memory"
    paths: list[Path] = []
    for folder_name in ("episodic", "impression"):
        folder = base / folder_name
        if not folder.is_dir():
            continue
        paths.extend(sorted(p for p in folder.glob("*.txt") if p.name not in {"notes.txt", "event.txt"}))
    keyed: list[tuple[int, str]] = []
    newest: str = ""
    for path in paths:
        for block in _sitting_blocks(path):
            newest = block[:900]
            keys_line = ""
            found = _KEYS_LINE.search(block)
            if found:
                keys_line = found.group(1)
            keys_hit = _keys_in(keys_line, text)
            if keys_hit:
                keyed.append((sum(len(k) for k in keys_hit), block[:900]))
    if keyed:
        keyed.sort(key=lambda item: item[0], reverse=True)
        out: list[str] = []
        seen: set[str] = set()
        for _, block in keyed:
            head = block[:80]
            if head in seen:
                continue
            seen.add(head)
            out.append(block)
            if len(out) >= max_sittings:
                break
        return out
    if intent and newest:
        return [newest]
    return []


def format_for_prompt(persona_id: str | None, scan_text: str) -> str:
    parts: list[str] = []
    facts = select_semantic(persona_id, scan_text)
    if facts:
        parts.append("【已采纳的事实】")
        parts.append("当作已知。不要主动念清单。")
        parts.extend(facts)
    sittings = select_sittings(persona_id, scan_text)
    if sittings:
        parts.append("【那次印象】")
        parts.append("对方在提后来发生的一场。按印象说，不要贴对白。")
        parts.extend(sittings)
    return "\n".join(parts).strip()
