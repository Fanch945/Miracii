"""Always-on tick: sleep until a deadline, wake on chat UDP, then consolidate / plan."""

from __future__ import annotations

import json
import socket
import time
from datetime import datetime
from pathlib import Path

from . import clock, persona, supervisor, wake
from .files import DATA_DIR
from .keys import load_keys

ROOT = Path(__file__).resolve().parent.parent
TICK_LOG = DATA_DIR / "heartbeat.log"
MAX_SLEEP = 3600.0


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


def _ask_sumapi(skill: str, packet: dict, key) -> dict:
    import httpx

    messages = [
        {"role": "system", "content": skill},
        {"role": "user", "content": json.dumps(packet, ensure_ascii=False)},
    ]
    payload = {
        "model": "deepseek-v4-flash",
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 400,
        "thinking": {"type": "disabled"},
    }
    headers = {"Authorization": f"Bearer {key.secret}", "Content-Type": "application/json"}
    with httpx.Client(timeout=45.0) as client:
        res = client.post("https://api.deepseek.com/chat/completions", headers=headers, json=payload)
        res.raise_for_status()
        text = res.json()["choices"][0]["message"]["content"]
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.replace("json", "", 1).strip()
    return json.loads(text)


def tick_once(persona_id: str | None = None, phase: str = "idle") -> dict:
    pid = persona_id or persona.active_id()
    packet = supervisor.build_packet(pid, phase=phase)
    skill = supervisor.load_skill()
    key = _sumapi()
    if key:
        try:
            decision = _ask_sumapi(skill, packet, key)
        except Exception as exc:
            _log(f"sumapi failed: {exc}; fallback {phase}")
            decision = supervisor.default_decision(packet)
    else:
        decision = supervisor.default_decision(packet)
        _log(f"no sumapi; rule-based phase={phase}")
    if phase in {"stm", "routine", "daily", "weekly"}:
        clock.fallback_consolidate(pid, phase)
    supervisor.apply_decision(pid, decision)
    clock.mark_fired(pid, phase)
    _log(
        json.dumps(
            {
                "persona": pid,
                "phase": phase,
                **{
                    k: decision.get(k)
                    for k in ("speak", "reason", "topic", "compact_working", "to_impression", "to_schema")
                },
                "tasks": decision.get("tasks") or [],
            },
            ensure_ascii=False,
        )
    )
    if decision.get("speak"):
        _log("speak proposed (not pushed to chat UI yet): " + str(decision.get("topic")))
    return decision


def run_forever() -> None:
    pid = persona.active_id()
    sched = supervisor.parse_schedule(pid)
    _log(
        f"heartbeat start persona={pid} stm={sched.get('stm_minutes')}m "
        f"routine={sched.get('routine_minutes')}m interrupt-sleep"
    )
    _log("sleep until STM/routine/daily/weekly; chat UDP wakes the sleeper")
    sock = wake.bind()
    try:
        while True:
            phase = clock.due_phase(pid)
            if phase:
                tick_once(pid, phase=phase)
                continue
            deadline = clock.next_deadline(pid)
            seconds = (deadline - clock._now()).total_seconds()
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
    run_forever()
