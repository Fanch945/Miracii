from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import persona
from .files import DATA_DIR, KEEPER_LOG, TRANSCRIPT_DIR

SUPERVISOR_LOG = DATA_DIR / "supervisor.jsonl"

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


def _keeper_tail(persona_id: str, n: int = 8) -> str:
    """Keeper log is shared. Only this persona's save lines go into the packet."""
    if not KEEPER_LOG.exists():
        return ""
    mark = f"  saved  {persona_id}  "
    lines = [line for line in KEEPER_LOG.read_text(encoding="utf-8", errors="replace").splitlines() if mark in line]
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
        "notify": True,
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
                elif key == "notify":
                    data[key] = value.strip().lower() not in {"off", "no", "false", "0"}
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


def _transcript_tail(persona_id: str, n: int = 20) -> str:
    folder = TRANSCRIPT_DIR / persona_id
    if not folder.exists():
        return ""
    files = sorted(folder.glob("*.txt"))
    if not files:
        return ""
    lines = files[-1].read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:]).strip()


def _impression_tail(persona_id: str, n_chars: int = 1200) -> str:
    folder = persona.folder(persona_id) / "memory" / "impression"
    if not folder.exists():
        return ""
    files = [p for p in sorted(folder.glob("*.txt")) if p.name != "notes.txt"]
    if not files:
        return ""
    text = files[-1].read_text(encoding="utf-8", errors="replace")
    if len(text) > n_chars:
        return "…\n" + text[-n_chars:]
    return text.strip()


def impression_corpus(persona_id: str, *, days: int = 2, n_chars: int = 4500) -> str:
    """Last N calendar days of impression files, oldest first. Daily/weekly source."""
    folder = persona.folder(persona_id) / "memory" / "impression"
    if not folder.exists():
        return ""
    today = datetime.now().astimezone().date()
    chunks: list[str] = []
    for offset in range(days - 1, -1, -1):
        path = folder / f"{(today - timedelta(days=offset)).isoformat()}.txt"
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8", errors="replace").strip()
        if body:
            chunks.append(body)
    text = "\n\n".join(chunks).strip()
    if len(text) > n_chars:
        return "…\n" + text[-n_chars:]
    return text


def parse_cast(persona_id: str | None = None) -> dict:
    """Who is who in transcripts vs impressions. Missing 对方： stays '对方'."""
    pid = persona_id or persona.active_id()
    persona_name = persona.display_name(pid)
    other = persona.other_name(pid)
    return {
        "persona": persona_name,
        "other": other,
        "transcript": {"你": other, "对方": persona_name},
        "impression": {"他": other, "她": persona_name},
        "rule": (
            f"你/他/哥哥=对方「{other}」。她/人格名字=「{persona_name}」。"
            f"对方的课业、宿舍、钱、课表、清单不要写成{persona_name}的。"
            "claim.who 必须是这两人之一的全名。"
        ),
    }


def _skip_hash(line: str) -> bool:
    s = line.strip()
    return (not s) or s.startswith("#")


def _jsonl_claims(path: Path, n: int = 12) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _skip_hash(raw):
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        claim = str((row or {}).get("claim") or "").strip()
        if claim:
            out.append(claim)
    return out[-n:]


def _file_fact_lines(path: Path, n: int = 20) -> list[str]:
    if not path.exists():
        return []
    lines = [
        ln.strip()
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not _skip_hash(ln)
    ]
    return lines[-n:]


def _canon_claim(text: str, cast: dict) -> str:
    t = re.sub(r"\s+", "", text or "")
    other = cast.get("other") or ""
    persona_n = cast.get("persona") or ""
    if other:
        for alias in (other, "哥哥", "他", "你"):
            if alias:
                t = t.replace(alias, "§U§")
    if persona_n:
        for alias in (persona_n, "妹妹", "她"):
            if alias:
                t = t.replace(alias, "§P§")
    return t


def _resolve_who(item: dict, cast: dict) -> str:
    other = cast.get("other") or "对方"
    persona_n = cast.get("persona") or ""
    raw = str(item.get("who") or "").strip()
    claim = str(item.get("claim") or "")
    user_aliases = {other, "你", "对方", "他", "哥", "哥哥", "用户"}
    persona_aliases = {persona_n, "她", "妹妹", "人格"}
    if raw in user_aliases:
        return other
    if raw in persona_aliases:
        return persona_n
    if "哥哥" in claim or (other and other in claim):
        return other
    if persona_n and (claim.startswith(persona_n) or persona_n in claim[:8]):
        return persona_n
    if raw:
        return raw
    return other


def _align_claim(claim: str, who: str, cast: dict) -> str:
    persona_n = cast.get("persona") or ""
    other = cast.get("other") or ""
    if not claim:
        return claim
    if who == other and persona_n and claim.startswith(persona_n):
        return other + claim[len(persona_n) :]
    if who == persona_n and other and claim.startswith(other):
        return persona_n + claim[len(other) :]
    return claim


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
        pending_count = len(
            [
                ln
                for ln in queue.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]
        )
    profile_lines = persona._read(base / "profile.txt").splitlines()[:12]
    cast = parse_cast(pid)
    facts_lines = _file_fact_lines(semantic / "facts.txt")
    people_lines = _file_fact_lines(semantic / "people.txt")
    cal_lines = _file_fact_lines(base / "memory" / "calendar.txt")
    pending_claims = _jsonl_claims(queue)
    return {
        "now": now.isoformat(timespec="minutes"),
        "phase": phase,
        "persona_id": pid,
        "quiet_now": in_quiet(now, str(schedule.get("quiet", ""))),
        "schedule": schedule,
        "cast": cast,
        "profile_excerpt": "\n".join(profile_lines),
        "working": persona.working_memory(pid),
        "transcript_tail": _transcript_tail(pid),
        "impression_tail": impression_corpus(pid) if phase in {"daily", "weekly"} else _impression_tail(pid),
        "semantic_index": names,
        "semantic_tail": people_lines + facts_lines,
        "calendar_tail": cal_lines,
        "learn_pending_count": pending_count,
        "learn_pending_tail": pending_claims,
        "last_user_hint": last_user,
        "last_assistant_hint": last_as,
        "log_tail": _keeper_tail(pid),
    }


def default_decision(packet: dict) -> dict:
    phase = packet.get("phase") or "idle"
    quiet = packet.get("quiet_now")
    speak = False
    reason = "quiet" if quiet else "none"
    if phase == "chat" and not quiet:
        reason = "chat"
    compact_working = phase == "stm" or len(packet.get("working") or "") > 800
    compact_episodic = phase == "daily"
    to_impression = phase == "routine"
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
        "impression": None,
        "working_note": None,
    }


def log_io(
    packet: dict,
    decision: dict,
    *,
    source: str,
    applied: dict | None = None,
    error: str | None = None,
    raw: str | None = None,
) -> None:
    """Append one tick's input packet and output decision. Local file, not chat."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "t": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": source,
        "persona_id": packet.get("persona_id"),
        "phase": packet.get("phase"),
        "packet": packet,
        "decision": decision,
        "applied": applied or {},
    }
    if error:
        record["error"] = error
    if raw:
        record["raw"] = raw[:4000]
    with SUPERVISOR_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def apply_decision(persona_id: str, decision: dict, *, phase: str = "") -> dict:
    """Adopt daily facts/calendar; queue patterns. Never rewrite profile or lore."""
    applied = {"queued": 0, "facts": 0, "people": 0, "calendar": 0, "skipped_dup": 0, "skipped_phase": 0}
    learn = decision.get("learn") or []
    if not learn:
        return applied
    if phase not in {"daily", "weekly"}:
        applied["skipped_phase"] = len([x for x in learn if isinstance(x, dict) and x.get("claim")])
        return applied

    cast = parse_cast(persona_id)
    base = persona.folder(persona_id) / "memory"
    queue = base / "learn_queue" / "pending.jsonl"
    facts_path = base / "semantic" / "facts.txt"
    people_path = base / "semantic" / "people.txt"
    cal_path = base / "calendar.txt"
    queue.parent.mkdir(parents=True, exist_ok=True)
    facts_path.parent.mkdir(parents=True, exist_ok=True)

    pending_known = {_canon_claim(c, cast) for c in _jsonl_claims(queue, n=200)}
    semantic_known: set[str] = set()
    for line in _file_fact_lines(facts_path, n=200) + _file_fact_lines(people_path, n=200):
        semantic_known.add(_canon_claim(line, cast))
        bits = [p.strip() for p in line.split("|")]
        if len(bits) >= 2:
            semantic_known.add(_canon_claim(bits[0] + bits[1], cast))
            semantic_known.add(_canon_claim(bits[1], cast))
    cal_known = {_canon_claim(line, cast) for line in _file_fact_lines(cal_path, n=200)}

    stamp = datetime.now().astimezone()
    day = stamp.date().isoformat()
    pending_rows: list[dict] = []
    fact_rows: list[str] = []
    people_rows: list[str] = []
    cal_rows: list[str] = []

    for item in learn:
        if not isinstance(item, dict) or not item.get("claim"):
            continue
        kind = str(item.get("kind") or "fact").strip().lower()
        target = str(item.get("target") or "").replace("\\", "/")
        who = _resolve_who(item, cast)
        claim = _align_claim(str(item.get("claim")).strip(), who, cast)
        keys = item.get("keys") or []
        if isinstance(keys, str):
            keys = [keys]
        key_s = ", ".join(str(k).strip() for k in keys if str(k).strip())
        try:
            conf = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        canon = _canon_claim(claim, cast)
        already = canon in semantic_known or canon in cal_known
        if already:
            applied["skipped_dup"] += 1
            continue

        risk = "lore" in target or target.endswith("profile.txt")
        pattern = kind in {"pattern", "schema"}
        promote = not risk and conf >= 0.55 and not pattern and kind in {"fact", "event"}

        wrote_cal = False
        if promote and (
            kind == "event"
            or "calendar" in target
            or item.get("when")
            or re.search(r"20\d{2}-\d{2}-\d{2}", claim)
        ):
            when = str(item.get("when") or "").strip()
            date_m = re.search(r"20\d{2}-\d{2}-\d{2}", when) or re.search(r"20\d{2}-\d{2}-\d{2}", claim)
            date_s = date_m.group(0) if date_m else day
            time_m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", when)
            time_s = time_m.group(0) if time_m else ""
            title = re.sub(r"20\d{2}-\d{2}-\d{2}", "", claim).strip(" ，,。；;")
            title = re.sub(r"^" + re.escape(who), "", title).strip(" ：:")
            line = f"{date_s} {time_s} {title}".replace("  ", " ").strip()
            cal_canon = _canon_claim(line, cast)
            if cal_canon not in cal_known and cal_canon not in semantic_known:
                cal_known.add(cal_canon)
                cal_rows.append(line)
                applied["calendar"] += 1
                wrote_cal = True
            if kind == "event" or "calendar" in target:
                if not wrote_cal:
                    applied["skipped_dup"] += 1
                continue

        if promote and kind == "fact" and "people" in target:
            line = f"{who} | {claim} | {day}"
            people_rows.append(line)
            semantic_known.add(canon)
            applied["people"] += 1
            continue

        if promote and kind == "fact":
            line = f"{who} | {claim} | {day} | {key_s}".rstrip(" |")
            fact_rows.append(line)
            semantic_known.add(canon)
            applied["facts"] += 1
            continue

        if canon in pending_known:
            applied["skipped_dup"] += 1
            continue
        pending_known.add(canon)
        pending_rows.append(
            {
                **item,
                "claim": claim,
                "who": who,
                "t": stamp.isoformat(timespec="seconds"),
                "source": item.get("source") or "supervisor",
            }
        )
        applied["queued"] += 1

    if pending_rows:
        with queue.open("a", encoding="utf-8") as handle:
            for row in pending_rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    _append_unique_lines(facts_path, fact_rows)
    _append_unique_lines(people_path, people_rows)
    _append_unique_lines(cal_path, cal_rows)
    return applied


def _append_unique_lines(path: Path, rows: list[str]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    existing = path.read_text(encoding="utf-8", errors="replace")
    with path.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        for row in rows:
            if row and row not in existing:
                handle.write(row + "\n")
                existing += row + "\n"
