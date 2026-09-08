---
name: persona-clock
description: Load local date/time and calendar.txt only when the user asks about the clock, weekday, date, or schedule. Do not inject this every chat turn. Use when editing clock/calendar retrieval for Miracii.
disable-model-invocation: true
---

# Persona clock

Not identity. Not always-on. Runtime (`app/when.py`) checks the recent user text; only a hit loads facts into that turn’s system prompt.

## Mutable files (edit anytime)

- `persona/<id>/memory/calendar.txt` — one event per line: `YYYY-MM-DD [HH:MM] title`
- `persona/<id>/memory/schedule.txt` — `timezone:` label only

The speaking model does not invent a calendar. Empty file = she says she has nothing written down.

## When to inject

Time/date (inject 【此刻】 only):

- 几点 / 几分 / 现在几点 / 现在是几 / 当前时间 / 什么时候了
- 星期几 / 周几 / 今天星期 / 今天几号 / 日期 / 今天是几

Calendar (inject calendar.txt window + any `YYYY-MM-DD` mentioned):

- 日历 / 日程 / 课表 / 有什么安排 / 有没有事 / 提醒

Do **not** trigger on bare 现在 / 今天 / 明天 — too common, would leak into every reply.

## What the character sees on a hit

A short fact block for this turn only. No skill markdown, no supervisor voice.

If they did not ask, omit the whole block so the model has nothing extra to parrot.
