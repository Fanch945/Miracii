"""Background file keeper: tails save log. Later can call a second API to grow secondary persona."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .files import DATA_DIR, KEEPER_LOG
from .keys import load_keys

ROOT = Path(__file__).resolve().parent.parent


def _summarizer_ready() -> bool:
    try:
        return any(key.provider.lower() == "sumapi" for key in load_keys())
    except (FileNotFoundError, ValueError, RuntimeError):
        return False


def tail_log() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not KEEPER_LOG.exists():
        KEEPER_LOG.write_text("", encoding="utf-8")

    print("Miracii file keeper")
    print(f"watching {KEEPER_LOG}")
    print("raw transcripts -> data/transcripts/<persona>/<date>.txt")
    print("secondary pending -> persona/<id>/secondary/pending.txt")
    print("memory -> persona/<id>/memory/")
    print("heartbeat -> python -m app.heartbeat")
    if _summarizer_ready():
        print("sumapi found: growth summarizer slot is ready (not auto-running yet)")
    else:
        print("growth summarizer idle: add sumapi=Alias=sk-... to APIkeys.txt later")
    print("----", flush=True)

    with KEEPER_LOG.open("r", encoding="utf-8") as handle:
        handle.seek(0, 2)
        while True:
            line = handle.readline()
            if line:
                print(line, end="", flush=True)
            else:
                time.sleep(0.4)


if __name__ == "__main__":
    try:
        tail_log()
    except KeyboardInterrupt:
        sys.exit(0)
