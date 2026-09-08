from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
KEEPER_LOG = DATA_DIR / "keeper.log"

ROLE_LABEL = {"user": "你", "assistant": "对方"}


def _ensure() -> None:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def append_turn(persona_id: str, speaker: str, role: str, content: str, created_at: str) -> Path:
    _ensure()
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    folder = TRANSCRIPT_DIR / persona_id
    folder.mkdir(parents=True, exist_ok=True)
    txt_path = folder / f"{day}.txt"
    jsonl_path = folder / f"{day}.jsonl"

    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    label = speaker if role == "assistant" else ROLE_LABEL.get(role, role)
    block = f"[{stamp}] {label}：{content}\n"
    with txt_path.open("a", encoding="utf-8") as handle:
        handle.write(block)

    record = {
        "created_at": created_at,
        "persona_id": persona_id,
        "role": role,
        "speaker": speaker,
        "content": content,
    }
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    _log_keeper(persona_id, role, content, txt_path)
    return txt_path


def _log_keeper(persona_id: str, role: str, content: str, path: Path) -> None:
    preview = content.replace("\n", " ").strip()
    if len(preview) > 80:
        preview = preview[:80] + "…"
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')}  saved  {persona_id}  {role}  {preview}  ->  {path}\n"
    with KEEPER_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line)


def append_pending(persona_id: str, text: str) -> Path:
    path = ROOT / "persona" / persona_id / "secondary" / "pending.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")
    _log_keeper(persona_id, "pending", text, path)
    return path


PERSONA_DIR = ROOT / "persona"
MAX_READ_BYTES = 400_000
PERSONA_FILENAMES = {
    "profile.txt",
    "voice_lines.txt",
    "examples.txt",
    "forbidden_style.txt",
    "source.txt",
}
MEMORY_FILENAMES = {"pending.txt", "notes.txt"}
READABLE_SUFFIX = {".txt", ".jsonl", ".log"}


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def _item(file_id: str, path: Path, group: str, title: str | None = None) -> dict:
    return {
        "id": file_id,
        "name": path.name,
        "title": title or path.name,
        "rel": path.relative_to(ROOT).as_posix(),
        "group": group,
        "size": path.stat().st_size,
        "mtime": _mtime(path),
    }


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def list_readable() -> list[dict]:
    _ensure()
    groups = [
        {"id": "log", "label": "日志", "files": []},
        {"id": "transcript", "label": "对话原文", "files": []},
        {"id": "memory", "label": "记忆", "files": []},
        {"id": "lore", "label": "设定", "files": []},
        {"id": "persona", "label": "人设", "files": []},
    ]
    by_id = {group["id"]: group for group in groups}

    if KEEPER_LOG.exists():
        by_id["log"]["files"].append(_item("log/keeper.log", KEEPER_LOG, "log", "保存记录"))

    if TRANSCRIPT_DIR.exists():
        for path in sorted(TRANSCRIPT_DIR.rglob("*")):
            if path.suffix.lower() in READABLE_SUFFIX and path.is_file():
                rel = path.relative_to(TRANSCRIPT_DIR).as_posix()
                by_id["transcript"]["files"].append(_item(f"transcript/{rel}", path, "transcript"))

    if PERSONA_DIR.exists():
        for persona_path in sorted(PERSONA_DIR.iterdir()):
            if not persona_path.is_dir() or persona_path.name.startswith("."):
                continue
            for name in sorted(MEMORY_FILENAMES):
                path = persona_path / "secondary" / name
                if path.is_file():
                    file_id = f"memory/{persona_path.name}/secondary/{name}"
                    title = f"{persona_path.name} / secondary/{name}"
                    by_id["memory"]["files"].append(_item(file_id, path, "memory", title))
            mem_root = persona_path / "memory"
            if mem_root.is_dir():
                for path in sorted(mem_root.rglob("*")):
                    if path.suffix.lower() in READABLE_SUFFIX and path.is_file():
                        rel = path.relative_to(mem_root).as_posix()
                        file_id = f"memory/{persona_path.name}/{rel}"
                        title = f"{persona_path.name} / {rel}"
                        by_id["memory"]["files"].append(_item(file_id, path, "memory", title))
            for name in sorted(PERSONA_FILENAMES):
                path = persona_path / name
                if path.is_file():
                    file_id = f"persona/{persona_path.name}/{name}"
                    title = f"{persona_path.name} / {name}"
                    by_id["persona"]["files"].append(_item(file_id, path, "persona", title))
            lore_root = persona_path / "lore"
            if lore_root.is_dir():
                for path in sorted(lore_root.rglob("*.txt")):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(lore_root).as_posix()
                    file_id = f"lore/{persona_path.name}/{rel}"
                    title = f"{persona_path.name} / {rel}"
                    by_id["lore"]["files"].append(_item(file_id, path, "lore", title))
    return groups


def resolve_readable(file_id: str) -> Path:
    if "/" not in file_id or ".." in file_id or "\\" in file_id:
        raise ValueError("无效文件")
    kind, rest = file_id.split("/", 1)
    if not rest or rest.startswith("/"):
        raise ValueError("无效文件")

    if kind == "log":
        if rest != "keeper.log":
            raise ValueError("无效文件")
        path = KEEPER_LOG
    elif kind == "transcript":
        path = (TRANSCRIPT_DIR / rest).resolve()
        if not _under(path, TRANSCRIPT_DIR):
            raise ValueError("无效文件")
    elif kind == "memory":
        path = _resolve_memory(rest)
    elif kind == "persona":
        persona_id, name = _split_once(rest)
        if name not in PERSONA_FILENAMES:
            raise ValueError("无效文件")
        path = (PERSONA_DIR / persona_id / name).resolve()
        if not _under(path, PERSONA_DIR):
            raise ValueError("无效文件")
    elif kind == "lore":
        path = _resolve_lore(rest)
    else:
        raise ValueError("无效文件")

    if not path.is_file() or path.suffix.lower() not in READABLE_SUFFIX:
        raise FileNotFoundError("文件不存在")
    return path


def _split_once(rest: str) -> tuple[str, str]:
    if "/" not in rest:
        raise ValueError("无效文件")
    left, right = rest.split("/", 1)
    if not left or not right or "/" in right:
        raise ValueError("无效文件")
    return left, right


def _resolve_memory(rest: str) -> Path:
    parts = rest.split("/")
    if any(part in ("", ".", "..") for part in parts) or len(parts) < 2:
        raise ValueError("无效文件")
    persona_id = parts[0]
    tail = parts[1:]
    root = PERSONA_DIR / persona_id
    if tail[0] == "secondary":
        if len(tail) != 2 or tail[1] not in MEMORY_FILENAMES:
            raise ValueError("无效文件")
        path = (root / "secondary" / tail[1]).resolve()
        if not _under(path, root / "secondary"):
            raise ValueError("无效文件")
        return path
    path = (root / "memory" / Path(*tail)).resolve()
    if not _under(path, root / "memory"):
        raise ValueError("无效文件")
    return path


def _resolve_lore(rest: str) -> Path:
    parts = rest.split("/")
    if any(part in ("", ".", "..") for part in parts) or len(parts) < 2:
        raise ValueError("无效文件")
    persona_id = parts[0]
    tail = parts[1:]
    if tail == ["always.txt"] or tail == ["notes.txt"]:
        path = (PERSONA_DIR / persona_id / "lore" / tail[0]).resolve()
    elif tail[0] == "entries" and len(tail) >= 2 and tail[-1].endswith(".txt"):
        path = (PERSONA_DIR / persona_id / "lore" / Path(*tail)).resolve()
    else:
        raise ValueError("无效文件")
    if not _under(path, PERSONA_DIR / persona_id / "lore"):
        raise ValueError("无效文件")
    return path


def read_readable(file_id: str) -> dict:
    path = resolve_readable(file_id)
    raw = path.read_bytes()
    truncated = False
    if len(raw) > MAX_READ_BYTES:
        raw = raw[-MAX_READ_BYTES:]
        truncated = True
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        text = "…（仅显示末尾）\n" + text
    return {
        "id": file_id,
        "name": path.name,
        "rel": path.relative_to(ROOT).as_posix(),
        "truncated": truncated,
        "content": text,
    }
