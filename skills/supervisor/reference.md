# Supervisor packet and memory map

The chat model (dsapi Zero) speaks. This supervisor (sumapi, later) only schedules and files.

## Files the tick may read

| path | role |
|---|---|
| `persona/<id>/profile.txt` | identity; supervisor gets a short extract only |
| `persona/<id>/memory/working.txt` | STM / working context |
| `persona/<id>/memory/impression/` | blurred gist after ~30 min; scene + affect, not quotes |
| `persona/<id>/memory/episodic/` | event notes |
| `persona/<id>/memory/semantic/` | durable facts |
| `persona/<id>/memory/learn_queue/pending.jsonl` | proposed writes |
| `persona/<id>/memory/schedule.txt` | stm/routine, quiet, quota; pomodoro_* = task duration hints only |
| `persona/<id>/lore/` | authored canon; supervisor does not rewrite |
| `data/transcripts/<id>/` | raw dialogue |
| `data/keeper.log` | save log |
| `data/heartbeat.log` | tick one-liners |
| `data/supervisor.jsonl` | full packet in, decision out, one JSON object per tick |

## Impression file shape

One sitting, one block, appended to `memory/impression/YYYY-MM-DD.txt`:

```
# routine 2026-09-12 14:47
那次：午饭后靠在一起。他让两点叫醒放松一下；她答应了，后来自己睡着，晚了十来分钟，有点心虚。还提过以后很难同实验室。
气氛：困、亲近、带点抱歉
keys: 计时, 午睡, 实验室
```

Runtime writes this from `decision.impression` on routine ticks. If the model omitted it or pasted truncated quotes, a local gist fallback is used (topics + mood, still no cut-off dialogue).

## Why a second model

The speaking model is biased to be helpful and to continue the conversation. A tiny supervisor with no chat UI is better at choosing **silence**, compacting files, and queuing facts. Same split as MemGPT's control flow vs utterance, and Generative Agents' planning vs dialogue.

## Papers behind the formats

See `docs/HOW_PERSONA_RUNS.txt`.
