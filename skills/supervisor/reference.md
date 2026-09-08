# Supervisor packet and memory map

The chat model (dsapi Zero) speaks. This supervisor (sumapi, later) only schedules and files.

## Files the tick may read

| path | role |
|---|---|
| `persona/<id>/profile.txt` | identity; supervisor gets a short extract only |
| `persona/<id>/memory/working.txt` | STM / working context |
| `persona/<id>/memory/impression/` | blurred gist after ~30 min |
| `persona/<id>/memory/episodic/` | event notes |
| `persona/<id>/memory/semantic/` | durable facts |
| `persona/<id>/memory/learn_queue/pending.jsonl` | proposed writes |
| `persona/<id>/memory/schedule.txt` | stm/routine, quiet, quota; pomodoro_* = task duration hints only |
| `persona/<id>/lore/` | authored canon; supervisor does not rewrite |
| `data/transcripts/<id>/` | raw dialogue |
| `data/keeper.log` | save log |

## Why a second model

The speaking model is biased to be helpful and to continue the conversation. A tiny supervisor with no chat UI is better at choosing **silence**, compacting files, and queuing facts. Same split as MemGPT's control flow vs utterance, and Generative Agents' planning vs dialogue.

## Papers behind the formats

See `docs/HOW_PERSONA_RUNS.txt`.
