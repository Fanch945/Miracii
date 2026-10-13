"""Retrieve adopted memory into the speaker prompt. Lore stays in lore.py.

P0.5 — candidates are ranked by `relevance × decay × novelty`, where the last two come from the
star map sidecar (`app/starmap.py`). With no sidecar both factors are 1.0, so the ranking falls
back to exactly the keyword scoring this module used before; the star map is an upgrade, not a
dependency.

Brightness is deliberately **not** used here: rehearsal brightens a memory in the report, while
ranking must not reward a line for having come up often.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import persona, starmap
from .lore import STORY_INTENT

_KEY_SPLIT = re.compile(r"[,，、;；]")
_KEYS_LINE = re.compile(r"^keys:\s*(.+)$", re.I | re.M)

MAX_HITS = 8
MAX_SITTINGS = 2
BLOCK_CHARS = 900
# The same block can live in two places: impression is the live gist, episodic the archived copy.
KIND_PREFERENCE = ("impression", "episodic")


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


def _priority(relevance: float, star: dict | None) -> float:
    """Keyword relevance, then aged and de-duplicated by familiarity. No star -> unchanged."""
    if not star:
        return float(relevance)
    return relevance * starmap.decay_factor(star) * starmap.novelty_factor(star.get("rc", 0))


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


def select_semantic(persona_id: str | None, scan_text: str, *, max_hits: int = MAX_HITS) -> list[str]:
    rows = load_semantic_lines(persona_id)
    text = (scan_text or "").casefold()
    if not rows or not text.strip():
        return []
    stars = starmap.lookup(persona_id)
    scored: list[tuple[float, int, str]] = []
    for index, row in enumerate(rows):
        keys_hit = _keys_in(row["keys"], text)
        who_hit = 2 if row["who"] and row["who"].casefold() in text else 0
        relevance = len(keys_hit) * 3 + who_hit
        if relevance <= 0:
            continue
        star = stars.get(starmap.fingerprint(row["line"]))
        scored.append((_priority(relevance, star), index, row["line"]))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    seen: set[str] = set()
    out: list[str] = []
    for _, _, line in scored:
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
        if len(out) >= max_hits:
            break
    return out


def _sitting_blocks(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    chunks = re.split(r"\n(?=# )", text)
    return [c.strip() for c in chunks if c.strip() and not c.strip().startswith("# 格式") and "notes.txt" not in path.name]


def _sitting_candidates(persona_id: str | None, text: str) -> list[tuple[float, str, str]]:
    """`(priority, block, kind)` for every sitting whose `keys:` line matches."""
    stars = starmap.lookup(persona_id)
    base = persona.folder(persona_id) / "memory"
    entries: list[tuple[float, str, str]] = []
    for folder, kind in (("impression", "impression"), ("episodic", "episodic")):
        directory = base / folder
        if not directory.is_dir():
            continue
        for path in sorted(p for p in directory.glob("*.txt") if p.name not in {"notes.txt", "event.txt"}):
            for block in _sitting_blocks(path):
                found = _KEYS_LINE.search(block)
                if not found:
                    continue
                hits = _keys_in(found.group(1), text)
                if not hits:
                    continue
                relevance = sum(len(k) for k in hits)
                star = stars.get(starmap.fingerprint(block))
                entries.append((_priority(relevance, star), block, kind))
    return entries


def _wins(kind: str, score: float, best_kind: str, best_score: float) -> bool:
    rank = {name: index for index, name in enumerate(KIND_PREFERENCE)}
    mine, theirs = rank.get(kind, len(rank)), rank.get(best_kind, len(rank))
    if mine != theirs:
        return mine < theirs
    return score > best_score


def _arbitrate(entries: list[tuple[float, str, str]], limit: int) -> list[str]:
    """One block per fingerprint: prefer the live copy, then the higher priority."""
    best: dict[str, tuple[float, str, str]] = {}
    for score, block, kind in entries:
        key = starmap.fingerprint(block)
        current = best.get(key)
        if current is None or _wins(kind, score, current[2], current[0]):
            best[key] = (score, block, kind)
    out: list[str] = []
    seen: set[str] = set()
    for _, block, _kind in sorted(best.values(), key=lambda item: -item[0]):
        head = block[:80]
        if head in seen:
            continue
        seen.add(head)
        out.append(block[:BLOCK_CHARS])
        if len(out) >= limit:
            break
    return out


def _drop_contained(lines: list[str], blocks: list[str]) -> list[str]:
    """A fact already spelled out inside an injected scene does not need its own line."""
    if not lines or not blocks:
        return lines
    hay = " ".join(" ".join(block.split()) for block in blocks)
    return [line for line in lines if " ".join(line.split()) not in hay]


def select_sittings(persona_id: str | None, scan_text: str, *, max_sittings: int = MAX_SITTINGS) -> list[str]:
    """Impression / episodic blocks when the human asks about 'that time' or hits keys."""
    text = (scan_text or "").casefold()
    if not text.strip():
        return []
    entries = _sitting_candidates(persona_id, text)
    if entries:
        return _arbitrate(entries, max_sittings)
    intent = any(word.casefold() in text for word in STORY_INTENT)
    if not intent:
        return []
    newest = _newest_sitting(persona_id)
    return [newest] if newest else []


def _newest_sitting(persona_id: str | None, *, max_chars: int = BLOCK_CHARS) -> str:
    base = persona.folder(persona_id) / "memory"
    newest = ""
    for folder in ("episodic", "impression"):
        directory = base / folder
        if not directory.is_dir():
            continue
        for path in sorted(p for p in directory.glob("*.txt") if p.name not in {"notes.txt", "event.txt"}):
            for block in _sitting_blocks(path):
                newest = block[:max_chars]
    return newest


def _note_injections(persona_id: str | None, *groups: list[str]) -> None:
    """Remember what actually reached the prompt. Bookkeeping must never break a reply."""
    try:
        fingerprints = [starmap.fingerprint(item) for group in groups for item in group]
        if fingerprints:
            starmap.bump_many(persona_id, fingerprints)
    except Exception:
        pass


def format_for_prompt(persona_id: str | None, scan_text: str) -> str:
    facts = select_semantic(persona_id, scan_text)
    sittings = select_sittings(persona_id, scan_text)
    kept = _drop_contained(facts, sittings)
    _note_injections(persona_id, kept, sittings)

    parts: list[str] = []
    if kept:
        parts.append("【已采纳的事实】")
        parts.append("当作已知。不要主动念清单。")
        parts.extend(kept)
    if sittings:
        parts.append("【那次印象】")
        parts.append("对方在提后来发生的一场。按印象说，不要贴对白。")
        parts.extend(sittings)
    return "\n".join(parts).strip()
