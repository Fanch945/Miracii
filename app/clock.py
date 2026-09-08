"""Multi-scale consolidation clock: chat resets 5 min STM, then 30 min routine, then day/week."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import persona
from .files import DATA_DIR, TRANSCRIPT_DIR
from .wake import ping as wake_ping
from .supervisor import parse_schedule

STATE_PATH = DATA_DIR / "clock.json"
_TURN = re.compile(r"^\[.*?\]\s*(.+?)：(.+)$")
_STAGE = re.compile(r"（[^）]*）|\([^)]*\)")


def _now() -> datetime:
    return datetime.now().astimezone()


def _load() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def on_chat(persona_id: str) -> dict:
    """Each new utterance: encoding / attention. Reset the 5-minute rehearsal window."""
    now = _now()
    sched = parse_schedule(persona_id)
    stm_min = int(sched.get("stm_minutes") or 5)
    state = _load()
    state.update(
        {
            "persona_id": persona_id,
            "last_chat_at": now.isoformat(timespec="seconds"),
            "stm_due_at": (now + timedelta(minutes=stm_min)).isoformat(timespec="seconds"),
            "stm_done": False,
            "routine_done": False,
            "in_conversation": True,
        }
    )
    state.pop("routine_due_at", None)
    _save(state)
    wake_ping()
    return state


def next_deadline(persona_id: str) -> datetime:
    """Earliest time due_phase might change. Chat UDP can wake sooner."""
    now = _now()
    state = _load()
    candidates: list[datetime] = []
    stm_due = _parse(state.get("stm_due_at"))
    if stm_due and not state.get("stm_done"):
        candidates.append(stm_due)
    if state.get("stm_done") and not state.get("routine_done"):
        routine_due = _parse(state.get("routine_due_at"))
        if routine_due:
            candidates.append(routine_due)

    today_23 = now.replace(hour=23, minute=0, second=0, microsecond=0)
    if state.get("last_daily") != now.date().isoformat():
        candidates.append(today_23 if now < today_23 else now)

    week = now.strftime("%G-W%V")
    if state.get("last_weekly") != week:
        days_ahead = 6 - now.weekday()
        sunday_23 = (now + timedelta(days=days_ahead)).replace(
            hour=23, minute=0, second=0, microsecond=0
        )
        if now.weekday() == 6 and now.hour >= 23:
            candidates.append(now)
        else:
            candidates.append(sunday_23)

    future = [t for t in candidates if t > now + timedelta(milliseconds=80)]
    if not future:
        return now + timedelta(minutes=5)
    return min(future)


def due_phase(persona_id: str) -> str | None:
    """What the heartbeat should run now. None = sleep until next_deadline or a chat ping."""
    now = _now()
    sched = parse_schedule(persona_id)
    state = _load()
    last_chat = _parse(state.get("last_chat_at"))
    stm_due = _parse(state.get("stm_due_at"))
    stm_done = bool(state.get("stm_done"))
    routine_due = _parse(state.get("routine_due_at"))
    last_daily = state.get("last_daily")
    last_weekly = state.get("last_weekly")

    idle = None if not last_chat else (now - last_chat).total_seconds() / 60
    stm_min = int(sched.get("stm_minutes") or 5)

    if last_chat and idle is not None and idle < stm_min:
        return None

    if stm_due and now >= stm_due and not stm_done:
        return "stm"

    if stm_done and not state.get("routine_done") and routine_due and now >= routine_due:
        return "routine"

    today = now.date().isoformat()
    if last_daily != today and now.hour >= 23:
        return "daily"

    week = now.strftime("%G-W%V")
    if last_weekly != week and now.weekday() == 6 and now.hour >= 23:
        return "weekly"

    return None


def mark_fired(persona_id: str, phase: str) -> None:
    now = _now()
    sched = parse_schedule(persona_id)
    state = _load()
    state["persona_id"] = persona_id
    if phase == "stm":
        routine_min = int(sched.get("routine_minutes") or 30)
        state["stm_done"] = True
        state["in_conversation"] = False
        state["last_stm_at"] = now.isoformat(timespec="seconds")
        state["routine_due_at"] = (now + timedelta(minutes=routine_min)).isoformat(timespec="seconds")
        state["routine_done"] = False
    elif phase == "routine":
        state["last_routine_at"] = now.isoformat(timespec="seconds")
        state["routine_done"] = True
        state.pop("routine_due_at", None)
    elif phase == "daily":
        state["last_daily"] = now.date().isoformat()
    elif phase == "weekly":
        state["last_weekly"] = now.strftime("%G-W%V")
    _save(state)


def last_transcript_excerpt(persona_id: str, n: int = 8) -> str:
    folder = TRANSCRIPT_DIR / persona_id
    if not folder.exists():
        return ""
    files = sorted(folder.glob("*.txt"))
    if not files:
        return ""
    lines = files[-1].read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:]).strip()


def _fingerprint(text: str) -> str:
    body = "\n".join(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    )
    if not body:
        return ""
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def _blur(text: str) -> str:
    """Turn rehearsal copies into a short impression. Do not keep full quotes."""
    bits: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _TURN.match(line)
        if match:
            who, said = match.group(1), _STAGE.sub("", match.group(2)).strip()
            said = re.sub(r"\s+", " ", said).strip()
            if len(said) > 48:
                said = said[:48] + "…"
            if who in {"你"}:
                bits.append("你：" + said)
            else:
                bits.append("她：" + said)
        elif not line.startswith("["):
            bits.append(line[:60])
    if not bits:
        return ""
    # keep a few beats, joined — this is the blur, not the transcript
    uniq: list[str] = []
    for bit in bits:
        if bit not in uniq:
            uniq.append(bit)
    return "；".join(uniq[-6:])


def fallback_consolidate(persona_id: str, phase: str) -> None:
    """No sumapi: still move traces along the cascade, never touch profile/lore."""
    base = persona.folder(persona_id) / "memory"
    working = base / "working.txt"
    stamp = _now().strftime("%Y-%m-%d %H:%M")
    excerpt = last_transcript_excerpt(persona_id, 6)

    if phase == "stm":
        base.mkdir(parents=True, exist_ok=True)
        block = f"\n# stm {stamp}\n"
        if excerpt:
            for line in excerpt.splitlines()[-4:]:
                block += line[:160] + "\n"
        with working.open("a", encoding="utf-8") as handle:
            handle.write(block)
        return

    if phase == "routine":
        text = working.read_text(encoding="utf-8") if working.exists() else ""
        source = text.strip() or excerpt
        gist = _blur(source)
        if not gist:
            return
        fp = _fingerprint(source)
        state = _load()
        if fp and fp == state.get("last_impression_fp"):
            return
        imp = base / "impression"
        imp.mkdir(parents=True, exist_ok=True)
        day = imp / f"{_now().date().isoformat()}.txt"
        with day.open("a", encoding="utf-8") as handle:
            handle.write(f"\n# routine {stamp}\n{gist}\n")
        state["last_impression_fp"] = fp
        _save(state)
        working.write_text(f"# 已写入印象 {stamp}\n{gist}\n", encoding="utf-8")
        return

    if phase in {"daily", "weekly"}:
        queue = base / "learn_queue" / "pending.jsonl"
        queue.parent.mkdir(parents=True, exist_ok=True)
        item = {
            "t": _now().isoformat(timespec="seconds"),
            "claim": f"{phase} 巩固：从 impression 提炼，尚未写入设定",
            "kind": "schema" if phase == "weekly" else "impression",
            "target": "lore/entries/learned.txt" if phase == "weekly" else "semantic/facts.txt",
            "keys": [phase],
            "confidence": 0.4,
            "source": f"clock-{phase}",
        }
        with queue.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
