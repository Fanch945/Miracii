"""Always-on tick: sleep until a deadline, wake on chat UDP, then consolidate / plan."""

from __future__ import annotations

import json
import re
import socket
import time
from datetime import datetime

from . import clock, db, files, notices, notify, persona, starmap, supervisor, wake, warmth
from .files import DATA_DIR
from .keys import load_keys

TICK_LOG = DATA_DIR / "heartbeat.log"
MAX_SLEEP = 3600.0


class SumapiError(RuntimeError):
    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


def _sumapi():
    try:
        return next(k for k in load_keys() if k.provider.lower() == "sumapi")
    except (StopIteration, FileNotFoundError, ValueError, RuntimeError):
        return None


def _log(line: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with TICK_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp}  {line}\n")
    print(line, flush=True)


def _ask_sumapi(skill: str, packet: dict, key, *, attempts: int = 3) -> dict:
    import httpx

    messages = [
        {"role": "system", "content": skill},
        {"role": "user", "content": json.dumps(packet, ensure_ascii=False)},
    ]
    payload = {
        "model": "deepseek-v4-flash",
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1100,
        "thinking": {"type": "disabled"},
    }
    headers = {"Authorization": f"Bearer {key.secret}", "Content-Type": "application/json"}
    last_exc: Exception | None = None
    last_raw = ""
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=45.0) as client:
                res = client.post("https://api.deepseek.com/chat/completions", headers=headers, json=payload)
                res.raise_for_status()
                text = res.json()["choices"][0]["message"]["content"]
            last_raw = (text or "").strip()
            text = last_raw
            if text.startswith("```"):
                text = text.strip("`")
                text = text.replace("json", "", 1).strip()
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("supervisor output is not an object")
            return parsed
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as exc:
            last_exc = exc
            _log(f"sumapi attempt {attempt}/{attempts} failed: {exc}")
            if attempt < attempts:
                time.sleep(1.5 * attempt)
    raise SumapiError(str(last_exc or "sumapi failed"), raw=last_raw)


def tick_once(persona_id: str | None = None, phase: str = "idle") -> dict:
    pid = persona_id or persona.active_id()
    try:
        starmap.sync(pid)
    except Exception as exc:  # the star map must never block a tick
        _log(f"starmap sync failed: {exc}")
    catch_up = phase in {"stm", "routine", "daily", "weekly"} and clock.phase_is_late(pid, phase)
    clock.begin_tick(pid, phase)
    packet = supervisor.build_packet(pid, phase=phase)
    if catch_up:
        packet["catch_up"] = True
    skill = supervisor.load_skill()
    key = _sumapi()
    source = "rule"
    error = None
    raw = None
    if key:
        try:
            decision = _ask_sumapi(skill, packet, key)
            source = "sumapi"
        except Exception as exc:
            error = str(exc)
            raw = getattr(exc, "raw", None)
            _log(f"sumapi failed: {exc}; fallback {phase}")
            decision = supervisor.default_decision(packet)
            source = "sumapi-fail"
    else:
        decision = supervisor.default_decision(packet)
        _log(f"no sumapi; rule-based phase={phase}")
    if not isinstance(decision, dict):
        decision = supervisor.default_decision(packet)
        source = "sumapi-fail"
        error = "decision was not an object"
    applied: dict = {}
    if phase in {"stm", "routine", "daily", "weekly"}:
        applied = clock.apply_consolidation(pid, phase, decision)
    if phase in {"daily", "weekly"}:
        if source != "sumapi":
            applied["learn_mode"] = source
        elif clock.learn_source_duplicate(pid, phase):
            applied["learn_mode"] = "duplicate-source"
        else:
            applied.update(supervisor.apply_decision(pid, decision, phase=phase))
            clock.mark_learn_source(pid, phase)
    else:
        applied.update(supervisor.apply_decision(pid, decision, phase=phase))
    if phase == "daily":
        applied["episodic"] = clock.write_daily_episode(pid, catch_up=catch_up)
    through_id = db.max_message_id(pid) if phase in {"stm", "routine", "daily"} else None
    through_at = db.max_created_at(pid) if phase == "stm" else None
    clock.mark_fired(pid, phase, catch_up=catch_up, through_id=through_id, through_at=through_at)
    supervisor.log_io(packet, decision, source=source, applied=applied, error=error, raw=raw)
    _log(
        json.dumps(
            {
                "persona": pid,
                "phase": phase,
                "catch_up": catch_up,
                "source": source,
                **{
                    k: decision.get(k)
                    for k in ("speak", "reason", "topic", "compact_working", "to_impression", "to_schema")
                },
                "learn": len(decision.get("learn") or []),
                "has_impression": bool(str(decision.get("impression") or "").strip()),
                "applied": applied,
                "tasks": decision.get("tasks") or [],
            },
            ensure_ascii=False,
        )
    )
    if phase in {"stm", "routine", "daily", "weekly"}:
        wrote = bool(applied.get("stm") or applied.get("impression") or applied.get("episodic") or applied.get("facts"))
        notices.push(pid, phase, wrote=wrote)
    if decision.get("speak"):
        uttered = deliver_speak(pid, decision, packet, force=False)
        if uttered:
            _log("spoke: " + uttered[:80])
        else:
            _log("speak proposed but not delivered: " + str(decision.get("topic")))
    return decision


def _may_speak(packet: dict, *, force: bool) -> str | None:
    if force:
        return None
    if clock.is_held(packet.get("persona_id") or persona.active_id()):
        return "hold"
    if packet.get("quiet_now"):
        return "quiet"
    if clock.live_window(packet.get("persona_id") or persona.active_id()):
        return "live-chat"
    sched = packet.get("schedule") or {}
    quota = int(sched.get("quota_speak_per_day") or 3)
    if clock.speak_count_today(packet.get("persona_id") or persona.active_id()) >= quota:
        return "quota"
    return None


def deliver_speak(persona_id: str, decision: dict, packet: dict, *, force: bool = False) -> str | None:
    """Turn a supervisor speak=true into one in-character line on the chat transcript."""
    blocked = _may_speak(packet, force=force)
    if blocked:
        _log(f"speak blocked: {blocked}")
        return None
    topic = str(decision.get("topic") or "").strip() or "惦记刚才的事"
    from .llm import LlmError, complete_reply

    system = persona.system_prompt(persona_id, scan_text=topic)
    system += (
        "\n\n【此刻】没有新的来信。你主动说一两句。"
        f"由头：{topic}。"
        "像平时那样开口。不要解释为什么突然说话，不要提监督、系统或任务。"
    )
    try:
        text = complete_reply(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": "（没有新消息。按【此刻】开口。）"},
            ]
        )
    except LlmError as exc:
        _log(f"speak generation failed: {exc}")
        return None
    if not text:
        return None
    speaker = persona.display_name(persona_id)
    saved = db.add_message(persona_id, "assistant", text)
    files.append_turn(persona_id, speaker, "assistant", text, saved["created_at"])
    clock.note_speak(persona_id)
    try:
        ping = notify.proactive(persona_id, speaker, text)
    except Exception as exc:  # the line is already saved; the reminder must not stop the tick
        _log(f"notify failed: {exc}")
    else:
        _log("ping: " + json.dumps(ping, ensure_ascii=False))
    return text


_STAMP = re.compile(r"\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")


def _parse_local(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def _minutes_since_message(persona_id: str) -> float | None:
    """距对方上一次开口过了多久——积温的"越等越急"靠它。

    优先读 jsonl（新格式，带 `created_at` 与 `role`）；老 txt 用行首时间戳兜底。
    """
    folder = files.TRANSCRIPT_DIR / persona_id
    if not folder.is_dir():
        return None
    candidates = sorted(folder.glob("*.jsonl")) or sorted(folder.glob("*.txt"))
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    stamp: datetime | None = None
    for line in newest.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            if "你：" in line:
                found = _STAMP.search(line)
                if found:
                    stamp = _parse_local(found.group(1).replace(" ", "T"))
            continue
        if isinstance(row, dict) and row.get("role") == "user" and row.get("created_at"):
            parsed = _parse_local(str(row["created_at"]))
            if parsed:
                stamp = parsed
    if stamp is None:
        return None
    return max(0.0, (datetime.now().astimezone() - stamp).total_seconds() / 60.0)


def run_warmth_tick(persona_id: str) -> dict:
    """空闲 tick：积温回答"现在想不想开口"，记忆那边照旧归巩固链。

    与巩固 tick 完全分开——这正是它以前被饿死的原因（supervisor 只在巩固截止点被叫醒，
    那些时刻它被要求整理记忆，而不是判断想不想说话）。
    """
    try:
        result = warmth.ambient_tick(persona_id, minutes_since_message=_minutes_since_message(persona_id))
    except Exception as exc:  # 引擎出问题不该拖垮心跳
        _log(f"warmth tick failed: {exc}")
        return {"elapsed": 0.0, "fired": [], "error": str(exc)}
    if result.get("elapsed", 0) <= 0:
        return result
    gates = [entry["gate"] for entry in result.get("fired", [])]
    _log(f"warmth {result['elapsed']}m: {'/'.join(gates) if gates else '无动作'} | {result.get('explain', '')}")
    if any(gate in {"contact", "force_contact"} for gate in gates):
        packet = supervisor.build_packet(persona_id, phase="idle")
        topic = (result.get("hook") or "").strip() or "惦记着刚才的事"
        if result.get("style"):
            topic = f"{topic}。写法：{result['style']}"
        uttered = deliver_speak(persona_id, {"speak": True, "topic": topic}, packet, force=False)
        _log("warmth spoke: " + (uttered[:80] if uttered else "(被挡下或没生成)"))
    return result


def run_forever() -> None:
    pid = persona.active_id()
    sched = supervisor.parse_schedule(pid)
    _log(
        f"heartbeat start persona={pid} stm={sched.get('stm_minutes')}m "
        f"routine={sched.get('routine_minutes')}m interrupt-sleep"
    )
    _log("sleep until STM/routine/daily/weekly; chat UDP wakes the sleeper")
    unfinished = clock.unfinished_tick(pid)
    if unfinished:
        _log(f"resume unfinished tick: {unfinished}")
        tick_once(pid, phase=unfinished)
    overdue = []
    while True:
        pid = persona.active_id()
        phase = clock.due_phase(pid)
        if not phase:
            break
        overdue.append(phase)
        tick_once(pid, phase=phase)
        if len(overdue) > 8:
            _log("catch-up stop: too many phases")
            break
    if overdue:
        _log(f"catch-up done: {' → '.join(overdue)}")
    sock = wake.bind()
    warm_guard = 0.0
    try:
        while True:
            pid = persona.active_id()
            phase = clock.due_phase(pid)
            if phase:
                tick_once(pid, phase=phase)
                continue
            deadline = clock.next_deadline(pid)
            seconds = (deadline - clock._now()).total_seconds()
            warm = warmth.seconds_until_due(pid)
            if warm <= 0:
                # 状态写不进去时 seconds_until_due 会一直返回 0——保险起见最多一分钟跑一次
                moment = time.monotonic()
                if moment - warm_guard >= 60.0:
                    warm_guard = moment
                    run_warmth_tick(pid)
                else:
                    time.sleep(1.0)
                continue
            seconds = min(seconds, warm)
            if seconds <= 0:
                time.sleep(0.2)
                continue
            woke = wake.wait(sock, min(seconds, MAX_SLEEP))
            if woke:
                _log("chat ping: STM window reset; not a reply (chat is the webpage)")
    except socket.error as exc:
        _log(f"wake socket failed: {exc}")
        raise
    finally:
        sock.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Miracii heartbeat")
    parser.add_argument("--once", nargs="?", const="idle", help="run one tick then exit (stm|routine|daily|weekly|idle)")
    parser.add_argument("--force-speak", action="store_true", help="after that tick, generate one proactive line (test)")
    args = parser.parse_args()
    if args.once:
        phase = args.once
        decision = tick_once(phase=phase)
        if args.force_speak:
            decision["speak"] = True
            decision["topic"] = decision.get("topic") or "测试：没有新消息时主动开口"
            packet = supervisor.build_packet(persona.active_id(), phase=phase)
            uttered = deliver_speak(persona.active_id(), decision, packet, force=True)
            print("force-speak:", uttered or "(empty)", flush=True)
    else:
        run_forever()
