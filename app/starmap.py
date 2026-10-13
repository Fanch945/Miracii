"""Star map metadata for the memory tree: a sidecar index, never a second copy of memory.

The memory files under `persona/<id>/` keep their format and their contents; everything
this module knows lives in `persona/<id>/memory/starmap.jsonl`, one star per line, append
only. Anchors carry a fingerprint of the block text, so hand-editing a memory file cannot
desynchronise the index — the edited block simply enters again as a new star.

Derived numbers (decay / recall bonus / brightness / magnitude) are always computed, never
stored: one source of truth. See docs/STARMAP.txt.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime
from pathlib import Path

from . import lore, persona

INDEX_NAME = "starmap.jsonl"
BLOCK_SPLIT = re.compile(r"\n(?=# )")
MOOD_LINE = re.compile(r"^气氛[:：]\s*(.+)$", re.M)
DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Six-band anchor for emotional weight: 0.0 plain fact ... 1.0 extreme impact.
EW_FALLBACK = 0.3
STRONG = ("崩溃", "哭", "吵", "害怕", "恐惧", "愤怒", "生气", "伤心", "绝望", "告别", "分离", "道歉")
CLEAR = ("开心", "高兴", "兴奋", "紧张", "担心", "委屈", "心疼", "感动", "失望", "惊喜", "烦躁", "心虚", "抱歉")
MILD = ("困", "累", "亲近", "轻松", "温柔", "拌嘴", "认真", "放松", "安静", "惦记")

# level 1 = fact/entry, 2 = one sitting, 3 = long arc (reserved)
LEVELS = {"semantic": 1, "lore": 1, "impression": 2, "episodic": 2}
STORY_LEVEL = 2


def index_path(persona_id: str | None = None) -> Path:
    return persona.folder(persona_id) / "memory" / INDEX_NAME


def fingerprint(text: str) -> str:
    """Whitespace-insensitive block identity: reflowing a file must not reshelve its stars."""
    flat = " ".join(str(text or "").split())
    return hashlib.sha1(flat.encode("utf-8")).hexdigest()[:12]


def mood_weight(mood: str | None) -> float:
    """Provisional local score for a `气氛：` line.

    The supervisor will score `ew` at write time (P2); until then this keeps the field
    populated from what the consolidation chain already writes.
    """
    text = (mood or "").strip()
    if not text:
        return EW_FALLBACK
    strong = sum(1 for word in STRONG if word in text)
    clear = sum(1 for word in CLEAR if word in text)
    mild = sum(1 for word in MILD if word in text)
    if strong >= 2:
        return 1.0
    if strong or clear >= 2:
        return 0.8
    if clear or mild >= 2:
        return 0.6
    if mild:
        return 0.4
    return 0.2


def decay_lambda(ew: float) -> float:
    """Four bands, same semantics as the reference implementation."""
    if ew >= 0.8:
        return 0.005
    if ew >= 0.6:
        return 0.01
    if ew >= 0.4:
        return 0.02
    return 0.04


def half_life_days(ew: float) -> float:
    return math.log(2) / decay_lambda(ew)


def recall_bonus(read_count: int) -> float:
    """Logarithmic, saturating at 0.3 around read_count ≈ 41.5."""
    return min(0.3, math.log(1 + max(0, int(read_count or 0))) * 0.08)


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return date.today()


def brightness(star: dict, today: date | None = None) -> float:
    """How bright this memory looks now. Rewards recall — used for feel, not for ranking."""
    now = today or date.today()
    days = max(0, (now - _as_date(star.get("last") or star.get("created"))).days)
    ew = float(star.get("ew", EW_FALLBACK))
    return min(1.0, math.exp(-decay_lambda(ew) * days) + recall_bonus(star.get("rc", 0)))


def magnitude(value: float) -> float:
    """Astronomical magnitude: smaller is brighter."""
    return round(6.5 - max(0.0, min(1.0, value)) * 5.5, 1)


def _mood_of(body: str) -> str:
    found = MOOD_LINE.search(body or "")
    return found.group(1) if found else ""


def _date_of(path: Path, body: str) -> str:
    found = DATE_IN_NAME.search(path.name)
    if found:
        return found.group(1)
    found = DATE_IN_NAME.search(body or "")
    if found:
        return found.group(1)
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


def _star(root: Path, path: Path, block: int, body: str, kind: str, level: int, src: str) -> dict:
    return {
        "file": path.relative_to(root).as_posix(),
        "block": block,
        "fp": fingerprint(body),
        "kind": kind,
        "level": level,
        "ew": mood_weight(_mood_of(body)),
        "rc": 0,
        "last": None,
        "created": _date_of(path, body),
        "src": src,
        "state": "active",
    }


def _blocks_of_file(root: Path, path: Path, kind: str, level: int, src: str) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    chunks = [c.strip() for c in BLOCK_SPLIT.split(text) if c.strip()]
    if not chunks:
        chunks = [text.strip()] if text.strip() else []
    return [_star(root, path, i, chunk, kind, level, src) for i, chunk in enumerate(chunks)]


def blocks(persona_id: str | None = None) -> list[dict]:
    """Every block currently in the memory tree, in a stable order. Reads only."""
    root = persona.folder(persona_id)
    stars: list[dict] = []

    memory = root / "memory"
    for folder, kind in (("impression", "impression"), ("episodic", "episodic")):
        base = memory / folder
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.txt")):
            if path.name in {"notes.txt", "event.txt"}:
                continue
            stars.extend(_blocks_of_file(root, path, kind, LEVELS[kind], f"{kind}:{path.name}"))

    semantic = memory / "semantic"
    for name in ("people.txt", "facts.txt"):
        path = semantic / name
        if not path.is_file():
            continue
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.strip().startswith("#") and "|" in line
        ]
        stars.extend(
            _star(root, path, i, line, "semantic", LEVELS["semantic"], f"semantic:{name}")
            for i, line in enumerate(lines)
        )

    for entry in lore.load_entries(persona_id):
        level = STORY_LEVEL if entry.form == "story" else LEVELS["lore"]
        rel = entry.path.relative_to(root).as_posix()
        stars.append(_star(root, entry.path, 0, entry.body, "lore", level, "authored"))

    return stars


def _read_rows(persona_id: str | None = None) -> list[dict]:
    path = index_path(persona_id)
    if not path.is_file():
        return []
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and "file" in row:
            rows.append(row)
    return rows


def _append_rows(persona_id: str | None, rows: list[dict]) -> None:
    if not rows:
        return
    path = index_path(persona_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def load(persona_id: str | None = None) -> dict[tuple[str, int], dict]:
    """Latest row per `(file, block)`; append-only file, last row wins."""
    latest: dict[tuple[str, int], dict] = {}
    for row in _read_rows(persona_id):
        latest[(str(row["file"]), int(row.get("block", 0)))] = row
    return latest


def sync(persona_id: str | None = None, *, today: date | None = None) -> dict:
    """Reconcile the sidecar with the memory tree. Never writes to a memory file.

    - unseen block        -> new star
    - same fingerprint    -> deferred, no write at all (an identical retry costs nothing)
    - changed fingerprint -> old star retired, block enters again as a new star
    - vanished block      -> retired
    """
    stamp = (today or date.today()).isoformat()
    known = load(persona_id)
    current = {(star["file"], star["block"]): star for star in blocks(persona_id)}
    writes: list[dict] = []
    seen: list[tuple[str, int]] = []
    added = retired = deferred = 0

    for key, star in current.items():
        seen.append(key)
        old = known.get(key)
        if old is None:
            writes.append(star)
            added += 1
            continue
        if old.get("fp") == star["fp"]:
            deferred += 1
            continue
        writes.append({**old, "state": "retired", "retired_on": stamp})
        writes.append({**star, "rc": int(old.get("rc") or 0), "last": old.get("last")})
        added += 1
        retired += 1

    for key, old in known.items():
        if key in seen or old.get("state") != "active":
            continue
        writes.append({**old, "state": "retired", "retired_on": stamp})
        retired += 1

    _append_rows(persona_id, writes)
    return {"added": added, "retired": retired, "deferred": deferred}


def bump(persona_id: str | None, file: str, block: int, *, today: date | None = None) -> bool:
    """Record one retrieval hit. Appends a new row; the old one stays for history."""
    stamp = (today or date.today()).isoformat()
    known = load(persona_id)
    old = known.get((file, int(block)))
    if old is None or old.get("state") != "active":
        return False
    _append_rows(persona_id, [{**old, "rc": int(old.get("rc") or 0) + 1, "last": stamp}])
    return True


def project(persona_id: str | None = None, *, today: date | None = None) -> list[dict]:
    """Read-only view: every active star with its current brightness and magnitude."""
    now = today or date.today()
    out: list[dict] = []
    for star in load(persona_id).values():
        if star.get("state") != "active":
            continue
        value = brightness(star, now)
        out.append({**star, "brightness": round(value, 4), "mag": magnitude(value)})
    return out


def report(persona_id: str | None = None, *, limit: int = 8, today: date | None = None) -> str:
    """Plain-text projection: brightest and dimmest memories, for a human glance."""
    stars = project(persona_id, today=today)
    if not stars:
        return "星图：还没有星点（先跑一次 sync）"
    ordered = sorted(stars, key=lambda s: s["brightness"], reverse=True)
    lines = [f"星图：{len(stars)} 个活跃星点"]
    lines.append("最亮：")
    for star in ordered[:limit]:
        lines.append(f"  mag {star['mag']:>4}  ew {star['ew']:<4} rc {star['rc']:<3} {star['file']}#{star['block']}")
    lines.append("最暗：")
    for star in ordered[-limit:][::-1]:
        lines.append(f"  mag {star['mag']:>4}  ew {star['ew']:<4} rc {star['rc']:<3} {star['file']}#{star['block']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Miracii star map (sidecar index)")
    parser.add_argument("--sync", action="store_true", help="reconcile the index with the memory tree")
    parser.add_argument("--report", action="store_true", help="print the brightest and dimmest stars")
    parser.add_argument("--persona", default=None)
    options = parser.parse_args()
    pid = options.persona or persona.active_id()
    if options.sync:
        print(json.dumps(sync(pid), ensure_ascii=False))
    if options.report or not options.sync:
        print(report(pid))
