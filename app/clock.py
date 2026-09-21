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


def snapshot() -> dict:
    return dict(_load())


def restore(state: dict) -> None:
    _save(state)


def live_window(persona_id: str, now: datetime | None = None) -> bool:
    """True while the current chat's 5-minute STM window is still open."""
    now = now or _now()
    sched = parse_schedule(persona_id)
    state = _load()
    last_chat = _parse(state.get("last_chat_at"))
    if not last_chat:
        return False
    stm_min = int(sched.get("stm_minutes") or 5)
    return (now - last_chat).total_seconds() < stm_min * 60


def missed_daily(now: datetime, last_daily: str | None) -> bool:
    """True if a 23:00 daily was skipped, including overnight downtime."""
    today = now.date()
    yesterday = today - timedelta(days=1)
    if last_daily == today.isoformat():
        return False
    if now.hour >= 23:
        return True
    if not last_daily:
        return False
    try:
        last = datetime.strptime(last_daily, "%Y-%m-%d").date()
    except ValueError:
        return True
    return last < yesterday


def missed_weekly(now: datetime, last_weekly: str | None) -> bool:
    week = now.strftime("%G-W%V")
    if last_weekly == week:
        return False
    if now.weekday() == 6 and now.hour >= 23:
        return True
    # 周日白天等到 23:00；周一到周六才补上上周日没做成的 weekly。
    return bool(last_weekly and last_weekly != week and now.weekday() != 6)


def phase_is_late(persona_id: str, phase: str) -> bool:
    now = _now()
    state = _load()
    if phase == "stm":
        due = _parse(state.get("stm_due_at"))
        return bool(due and now - due > timedelta(minutes=1))
    if phase == "routine":
        due = _parse(state.get("routine_due_at"))
        return bool(due and now - due > timedelta(minutes=1))
    if phase == "daily":
        return now.hour < 23
    if phase == "weekly":
        return not (now.weekday() == 6 and now.hour >= 23)
    return False


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
    if missed_daily(now, state.get("last_daily")):
        candidates.append(now)
    elif state.get("last_daily") != now.date().isoformat():
        candidates.append(today_23 if now < today_23 else now)

    week = now.strftime("%G-W%V")
    if missed_weekly(now, state.get("last_weekly")):
        candidates.append(now)
    elif state.get("last_weekly") != week:
        days_ahead = 6 - now.weekday()
        sunday_23 = (now + timedelta(days=days_ahead)).replace(
            hour=23, minute=0, second=0, microsecond=0
        )
        candidates.append(now if (now.weekday() == 6 and now.hour >= 23) else sunday_23)

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
    stm_min = int(sched.get("stm_minutes") or 5)
    live = bool(last_chat and (now - last_chat).total_seconds() < stm_min * 60)

    # 当前这轮还在聊：不要把新话搅进落下的 stm/routine。日周读 impression，可以补做。
    if not live:
        if stm_due and now >= stm_due and not stm_done:
            return "stm"
        if stm_done and not state.get("routine_done") and routine_due and now >= routine_due:
            return "routine"

    if missed_daily(now, state.get("last_daily")):
        return "daily"
    if missed_weekly(now, state.get("last_weekly")):
        return "weekly"
    return None


def mark_fired(
    persona_id: str,
    phase: str,
    *,
    catch_up: bool = False,
    through_id: int | None = None,
    through_at: str | None = None,
) -> None:
    now = _now()
    sched = parse_schedule(persona_id)
    state = _load()
    state["persona_id"] = persona_id
    state.pop("tick_in_progress", None)
    if phase == "stm":
        routine_min = int(sched.get("routine_minutes") or 30)
        state["stm_done"] = True
        state["in_conversation"] = False
        state["last_stm_at"] = now.isoformat(timespec="seconds")
        anchor = _parse(state.get("stm_due_at")) if catch_up else now
        if anchor is None:
            anchor = now
        state["routine_due_at"] = (anchor + timedelta(minutes=routine_min)).isoformat(timespec="seconds")
        state["routine_done"] = False
        if through_id is not None:
            state["consolidated_through_id"] = through_id
        if through_at:
            state["consolidated_through_at"] = through_at
        elif through_id is not None:
            state["consolidated_through_at"] = now.isoformat(timespec="seconds")
    elif phase == "routine":
        state["last_routine_at"] = now.isoformat(timespec="seconds")
        state["routine_done"] = True
        state.pop("routine_due_at", None)
    elif phase == "daily":
        if catch_up and now.hour < 23:
            state["last_daily"] = (now.date() - timedelta(days=1)).isoformat()
        else:
            state["last_daily"] = now.date().isoformat()
    elif phase == "weekly":
        state["last_weekly"] = now.strftime("%G-W%V")
    _save(state)


def speak_count_today() -> int:
    state = _load()
    today = _now().date().isoformat()
    if state.get("speak_date") != today:
        return 0
    return int(state.get("speak_count") or 0)


def note_speak() -> int:
    state = _load()
    today = _now().date().isoformat()
    if state.get("speak_date") != today:
        state["speak_date"] = today
        state["speak_count"] = 0
    state["speak_count"] = int(state.get("speak_count") or 0) + 1
    _save(state)
    return int(state["speak_count"])


def begin_tick(persona_id: str, phase: str) -> None:
    state = _load()
    state["persona_id"] = persona_id
    state["tick_in_progress"] = phase
    _save(state)


def unfinished_tick(persona_id: str) -> str | None:
    state = _load()
    if state.get("persona_id") not in {None, persona_id}:
        return None
    phase = state.get("tick_in_progress")
    return phase if phase in {"stm", "routine", "daily", "weekly"} else None


def lock_status(persona_id: str) -> dict:
    state = _load()
    return {
        "locked_through_id": state.get("consolidated_through_id"),
        "locked_through_at": state.get("consolidated_through_at") or state.get("last_stm_at"),
        "last_stm_at": state.get("last_stm_at"),
        "stm_done": bool(state.get("stm_done")),
    }


def after_rewind(persona_id: str) -> None:
    """Unconsolidated tail removed: close the live window, keep the STM lock."""
    state = _load()
    lock_at = state.get("consolidated_through_at") or state.get("last_stm_at")
    state["persona_id"] = persona_id
    state["in_conversation"] = False
    if lock_at:
        state["last_chat_at"] = lock_at
        state["stm_done"] = True
        state.pop("stm_due_at", None)
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


_STOP = {
    "然后",
    "不过",
    "因为",
    "所以",
    "什么",
    "怎么",
    "我们",
    "你们",
    "他们",
    "自己",
    "一下",
    "这个",
    "那个",
    "只是",
    "还是",
    "已经",
    "没有",
    "不是",
    "可以",
    "觉得",
    "知道",
    "出来",
    "起来",
    "这样",
    "那样",
    "现在",
    "时候",
    "真的",
    "有点",
    "一点",
    "一些",
    "一直",
    "还要",
    "还没",
    "如果",
    "要是",
    "好像",
    "突然",
    "今天",
    "明天",
    "晚上",
}


def _tod_label(when: datetime) -> str:
    hour = when.hour
    if 5 <= hour < 11:
        return "早上"
    if 11 <= hour < 14:
        return "午饭前后"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 22:
        return "晚上"
    return "夜里"


def _utterances(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _TURN.match(line)
        if match:
            said = _STAGE.sub("", match.group(2)).strip()
            said = re.sub(r"\s+", " ", said).strip()
            if said:
                found.append((match.group(1), said))
        elif not line.startswith("["):
            found.append(("", line))
    return found


def _keys_from(texts: list[str], n: int = 5) -> list[str]:
    counts: dict[str, int] = {}
    for text in texts:
        for token in re.findall(r"[\u4e00-\u9fff]{2,6}", text):
            if token in _STOP:
                continue
            counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts, key=lambda token: (-min(len(token), 4), -counts[token], token))
    keys: list[str] = []
    for token in ranked:
        if any(token in seen or seen in token for seen in keys):
            continue
        keys.append(token)
        if len(keys) >= n:
            break
    return keys


def _mood_from(texts: list[str]) -> str:
    blob = "".join(texts)
    tags: list[str] = []
    if any(word in blob for word in ("对不起", "抱歉", "心虚", "睡过去")):
        tags.append("带点抱歉")
    if any(word in blob for word in ("睡", "困", "午休", "靠着")):
        tags.append("困")
    if any(word in blob for word in ("笑", "夸", "抱", "陪", "亲")):
        tags.append("亲近")
    if any(word in blob for word in ("紧张", "期待", "实验室", "课题")):
        tags.append("惦记正事")
    return "、".join(tags[:3]) or "平常"


def looks_like_quotes(text: str) -> bool:
    """True if this is truncated dialogue, not an impression gist."""
    if not text:
        return False
    if text.count("你：") >= 2 and ("她：" in text or "：" in text):
        return True
    if "；你：" in text or "；她：" in text:
        return True
    if text.count("…") >= 2 and "；" in text:
        return True
    return False


def gist_from_source(source: str, when: datetime | None = None) -> str:
    """Blur a sitting into 'what that time was like'. Never keep cut-off quotes."""
    when = when or _now()
    utterances = _utterances(source)
    saids = [said for _, said in utterances]
    keys = _keys_from(saids or [source])
    mood = _mood_from(saids or [source])
    tod = _tod_label(when)
    n = len(utterances)
    if n <= 0 and not source.strip():
        return ""
    topic = "、".join(keys) if keys else "闲聊"
    rounds = f"大约 {n} 来回" if n else "一小段"
    lines = [
        f"那次：{tod}的{rounds}。话题里有{topic}。细节已经对不上原话。",
        f"气氛：{mood}",
    ]
    if keys:
        lines.append("keys: " + ", ".join(keys))
    return "\n".join(lines)


def _impression_body(source: str, model_text: str | None, when: datetime) -> str:
    offered = model_text if isinstance(model_text, str) else ""
    offered = offered.strip()
    if offered.startswith("```"):
        offered = offered.strip("`")
        offered = re.sub(r"^json\s*", "", offered, flags=re.I).strip()
    if offered and not looks_like_quotes(offered):
        lines = [ln.rstrip() for ln in offered.splitlines() if not ln.strip().startswith("# routine")]
        return "\n".join(lines).strip()
    return gist_from_source(source, when)


def fallback_consolidate(persona_id: str, phase: str, decision: dict | None = None) -> dict:
    """Move traces along the cascade. Prefer supervisor prose; never touch profile/lore."""
    return apply_consolidation(persona_id, phase, decision or {})


def apply_consolidation(persona_id: str, phase: str, decision: dict) -> dict:
    """Write stm / impression from the tick. Daily/weekly facts come from decision.learn."""
    applied = {"stm": False, "impression": False, "learn_fallback": False, "mode": "skip"}
    base = persona.folder(persona_id) / "memory"
    working = base / "working.txt"
    stamp = _now().strftime("%Y-%m-%d %H:%M")
    excerpt = last_transcript_excerpt(persona_id, 8)

    if phase == "stm":
        base.mkdir(parents=True, exist_ok=True)
        fp = _fingerprint(excerpt)
        state = _load()
        if fp and fp == state.get("last_stm_fp"):
            applied["mode"] = "duplicate"
            return applied
        note = (decision.get("working_note") or "").strip()
        if note and not looks_like_quotes(note):
            block = f"\n# stm {stamp}\n{note}\n"
            applied["mode"] = "supervisor"
        else:
            block = f"\n# stm {stamp}\n"
            if excerpt:
                for line in excerpt.splitlines()[-4:]:
                    block += line[:160] + "\n"
            applied["mode"] = "rehearsal"
        with working.open("a", encoding="utf-8") as handle:
            handle.write(block)
        if fp:
            state["last_stm_fp"] = fp
            _save(state)
        applied["stm"] = True
        return applied

    if phase == "routine":
        text = working.read_text(encoding="utf-8") if working.exists() else ""
        source = text.strip() or excerpt
        body = _impression_body(source, decision.get("impression"), _now())
        if not body:
            return applied
        fp = _fingerprint(source)
        state = _load()
        if fp and fp == state.get("last_impression_fp"):
            applied["mode"] = "duplicate"
            return applied
        imp = base / "impression"
        imp.mkdir(parents=True, exist_ok=True)
        day = imp / f"{_now().date().isoformat()}.txt"
        with day.open("a", encoding="utf-8") as handle:
            handle.write(f"\n# routine {stamp}\n{body}\n")
        state["last_impression_fp"] = fp
        _save(state)
        working.write_text(f"# 已写入印象 {stamp}\n{body}\n", encoding="utf-8")
        applied["impression"] = True
        applied["mode"] = (
            "supervisor"
            if (decision.get("impression") or "").strip() and not looks_like_quotes(str(decision.get("impression")))
            else "gist"
        )
        return applied

    if phase in {"daily", "weekly"}:
        # Real claims belong in decision["learn"]. Do not write empty placeholders.
        applied["mode"] = "learn-only"
        return applied

    return applied
