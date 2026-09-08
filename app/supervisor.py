from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import persona
from .files import KEEPER_LOG, TRANSCRIPT_DIR

ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = ROOT / "skills" / "supervisor" / "SKILL.md"


def load_skill() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _tail(path: Path, n: int = 12) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def parse_schedule(persona_id: str) -> dict:
    raw = _read(persona.folder(persona_id) / "memory" / "schedule.txt")
    data = {
        "timezone": "Asia/Shanghai",
        "quiet": "00:30-08:30",
        "tick_minutes": 20,
        "quota_speak_per_day": 3,
        "min_idle_minutes": 45,
        "stm_minutes": 5,
        "routine_minutes": 30,
        "pomodoro_work": 25,
        "pomodoro_break": 5,
        "pomodoro_long_break": 15,
        "pomodoros_per_set": 4,
        "daily": [],
    }
    section = None
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "daily:":
            section = "daily"
            continue
        if line.startswith("- "):
            data["daily"].append(line[2:].strip())
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key in data and key != "daily":
                if key in {
                    "tick_minutes",
                    "quota_speak_per_day",
                    "min_idle_minutes",
                    "stm_minutes",
                    "routine_minutes",
                    "pomodoro_work",
                    "pomodoro_break",
                    "pomodoro_long_break",
                    "pomodoros_per_set",
                }:
                    try:
                        data[key] = int(value)
                    except ValueError:
                        pass
                else:
                    data[key] = value
    return data


def in_quiet(now: datetime, quiet: str) -> bool:
    try:
        start_s, end_s = quiet.split("-", 1)
        start = datetime.strptime(start_s.strip(), "%H:%M").time()
        end = datetime.strptime(end_s.strip(), "%H:%M").time()
    except ValueError:
        return False
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def last_transcript_times(persona_id: str) -> tuple[str | None, str | None]:
    folder = TRANSCRIPT_DIR / persona_id
    if not folder.exists():
        return None, None
    files = sorted(folder.glob("*.txt"))
    if not files:
        return None, None
    last_user = last_as = None
    for line in files[-1].read_text(encoding="utf-8", errors="replace").splitlines():
        if "】你：" in line or "] 你：" in line:
            last_user = line[:20]
        elif "：" in line:
            last_as = line[:20]
    return last_user, last_as


def build_packet(persona_id: str | None = None, phase: str = "idle") -> dict:
    pid = persona_id or persona.active_id()
    now = datetime.now().astimezone()
    base = persona.folder(pid)
    schedule = parse_schedule(pid)
    last_user, last_as = last_transcript_times(pid)
    semantic = base / "memory" / "semantic"
    names = []
    if semantic.exists():
        names = [p.name for p in sorted(semantic.glob("*.txt"))]
    queue = base / "memory" / "learn_queue" / "pending.jsonl"
    pending_count = 0
    if queue.exists():
        pending_count = len([ln for ln in queue.read_text(encoding="utf-8").splitlines() if ln.strip()])
    profile_lines = persona._read(base / "profile.txt").splitlines()[:12]
    return {
        "now": now.isoformat(timespec="minutes"),
        "phase": phase,
        "persona_id": pid,
        "quiet_now": in_quiet(now, str(schedule.get("quiet", ""))),
        "schedule": schedule,
        "profile_excerpt": "\n".join(profile_lines),
        "working": persona.working_memory(pid),
        "semantic_index": names,
        "learn_pending_count": pending_count,
        "last_user_hint": last_user,
        "last_assistant_hint": last_as,
        "log_tail": _tail(KEEPER_LOG, 8),
    }


def default_decision(packet: dict) -> dict:
    phase = packet.get("phase") or "idle"
    quiet = packet.get("quiet_now")
    speak = False
    reason = "quiet" if quiet else "none"
    if phase == "chat" and not quiet:
        reason = "chat"
    compact_working = phase == "stm" or len(packet.get("working") or "") > 800
    compact_episodic = phase in {"routine", "daily"}
    to_impression = phase in {"routine", "daily"}
    to_schema = phase == "weekly"
    if phase in {"stm", "routine", "daily", "weekly"}:
        reason = phase
    return {
        "speak": speak,
        "reason": reason,
        "topic": None,
        "compact_working": compact_working,
        "compact_episodic": compact_episodic,
        "to_impression": to_impression,
        "to_schema": to_schema,
        "learn": [],
        "tasks": [],
    }


def apply_decision(persona_id: str, decision: dict) -> None:
    """Queue learns only. Never rewrite profile or lore."""

    learn = decision.get("learn") or []
    if not learn:
        return
    queue = persona.folder(persona_id) / "memory" / "learn_queue" / "pending.jsonl"
    queue.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with queue.open("a", encoding="utf-8") as handle:
        for item in learn:
            if not isinstance(item, dict) or not item.get("claim"):
                continue
            item = {**item, "t": stamp, "source": item.get("source") or "supervisor"}
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
