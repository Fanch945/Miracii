"""Clock and calendar: load only when the user actually asks."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import persona

WEEKDAYS = "一二三四五六日"

# Strict on purpose: 「现在」「今天」太常见，不能当触发。
TIME_RE = re.compile(
    r"几点|几分|现在是几|当前时间|什么时候了|现在几点|"
    r"星期几|周几|今天星期|今天周几|今天几号|几月几|日期|"
    r"今天是几|几号了"
)
CAL_RE = re.compile(r"日历|日程|课表|有什么安排|有没有安排|有什么事|有没有事|有事吗|提醒")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def now_stamp() -> datetime:
    return datetime.now().astimezone()


def now_line(tz_name: str = "Asia/Shanghai") -> str:
    now = now_stamp()
    return (
        f"{now.strftime('%Y-%m-%d')} 星期{WEEKDAYS[now.weekday()]} "
        f"{now.strftime('%H:%M')}（{tz_name}）"
    )


def wanted(scan_text: str) -> dict[str, bool]:
    text = scan_text or ""
    return {
        "time": bool(TIME_RE.search(text)),
        "calendar": bool(CAL_RE.search(text) or DATE_RE.search(text)),
    }


def _parse_line(line: str) -> dict | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("- "):
        text = text[2:].strip()
    parts = text.split(None, 2)
    if len(parts) < 2:
        return None
    try:
        day = datetime.strptime(parts[0], "%Y-%m-%d").date()
    except ValueError:
        return None
    when = ""
    title = parts[1]
    if ":" in parts[1] and len(parts) >= 3:
        when = parts[1]
        title = parts[2]
    elif len(parts) >= 3:
        title = f"{parts[1]} {parts[2]}"
    return {"day": day, "when": when, "title": title}


def load_events(persona_id: str | None = None) -> list[dict]:
    path = persona.folder(persona_id) / "memory" / "calendar.txt"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        item = _parse_line(line)
        if item:
            events.append(item)
    events.sort(key=lambda row: (row["day"].isoformat(), row["when"] or "99:99"))
    return events


def upcoming_lines(persona_id: str | None = None, days: int = 14, limit: int = 12, scan_text: str = "") -> str:
    today = now_stamp().date()
    mentioned = set()
    for raw in DATE_RE.findall(scan_text or ""):
        try:
            mentioned.add(datetime.strptime(raw, "%Y-%m-%d").date())
        except ValueError:
            pass
    end = today + timedelta(days=days)
    rows: list[str] = []
    for item in load_events(persona_id):
        in_window = today <= item["day"] <= end
        if not in_window and item["day"] not in mentioned:
            continue
        if item["day"] == today:
            label = "今天"
        elif item["day"] == today + timedelta(days=1):
            label = "明天"
        else:
            label = f"{item['day'].isoformat()} 星期{WEEKDAYS[item['day'].weekday()]}"
        clock = f"{item['when']} " if item["when"] else ""
        rows.append(f"{label} {clock}{item['title']}".strip())
        if len(rows) >= limit:
            break
    return "\n".join(rows)


def prompt_block(
    persona_id: str | None = None,
    tz_name: str = "Asia/Shanghai",
    *,
    want_time: bool = False,
    want_calendar: bool = False,
    scan_text: str = "",
) -> str:
    if not want_time and not want_calendar:
        return ""
    parts = ["【时间 skill · 本回合才读】", "只答对方问的那一点。不要顺带报时，不要解释这是设定。"]
    if want_time:
        parts.extend(["", "【此刻】", now_line(tz_name)])
    if want_calendar:
        cal = upcoming_lines(persona_id, scan_text=scan_text)
        parts.extend(["", "【日历】", cal or "这几天 calendar.txt 里没有条目。没写的就说没记。"])
    return "\n".join(parts)
