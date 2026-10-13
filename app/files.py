from __future__ import annotations

import json
import re
from datetime import datetime, timezone
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


def retract_turn(persona_id: str, created_at: str) -> None:
    """Remove one saved turn (failed reply / resend). Does not touch older days."""
    trim_transcript_after(persona_id, created_at, inclusive=True)


def trim_transcript_after(persona_id: str, after_created_at: str | None, *, inclusive: bool = False) -> int:
    """Drop jsonl/txt lines newer than the lock. Keep the lock timestamp unless inclusive."""
    folder = TRANSCRIPT_DIR / persona_id
    if not folder.exists() or not after_created_at:
        if after_created_at is None:
            return _wipe_today(persona_id)
        return 0
    removed = 0
    for jsonl_path in sorted(folder.glob("*.jsonl")):
        raw = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
        kept: list[str] = []
        dropped: list[dict] = []
        for line in raw:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            stamp = rec.get("created_at") or ""
            rec_t = _aware(stamp)
            after_t = _aware(after_created_at)
            if rec_t and after_t:
                drop = rec_t > after_t or (inclusive and rec_t == after_t)
            else:
                drop = stamp > after_created_at or (inclusive and stamp == after_created_at)
            if drop:
                dropped.append(rec)
            else:
                kept.append(line)
        if not dropped:
            continue
        removed += len(dropped)
        jsonl_path.write_text(("\n".join(kept) + ("\n" if kept else "")), encoding="utf-8")
        _rewrite_txt_from_jsonl(jsonl_path)
    if removed:
        _log_keeper(persona_id, "retract", f"{removed} turns after {after_created_at}", folder)
    return removed


def _wipe_today(persona_id: str) -> int:
    folder = TRANSCRIPT_DIR / persona_id
    if not folder.exists():
        return 0
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    removed = 0
    for suffix in (".jsonl", ".txt"):
        path = folder / f"{day}{suffix}"
        if path.is_file():
            if suffix == ".jsonl":
                removed = len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()])
            path.unlink()
    return removed


def _rewrite_txt_from_jsonl(jsonl_path: Path) -> None:
    txt_path = jsonl_path.with_suffix(".txt")
    lines: list[str] = []
    for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        created = rec.get("created_at") or ""
        try:
            stamp = datetime.fromisoformat(created).astimezone().strftime("%H:%M:%S")
        except ValueError:
            stamp = created[11:19] if len(created) >= 19 else created
        role = rec.get("role") or ""
        speaker = rec.get("speaker") or ""
        label = speaker if role == "assistant" else ROLE_LABEL.get(role, role)
        lines.append(f"[{stamp}] {label}：{rec.get('content') or ''}")
    txt_path.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _aware(iso: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


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


_DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")


_PERSONA_ORDER = (
    "profile.txt",
    "voice_lines.txt",
    "examples.txt",
    "forbidden_style.txt",
    "source.txt",
)


def _chrono(item: dict) -> tuple:
    if item.get("group") == "persona":
        try:
            rank = _PERSONA_ORDER.index(item["name"])
        except ValueError:
            rank = 99
        return (0, f"{rank:02d}", item["rel"])
    found = _DATE_IN_NAME.search(item["rel"])
    if found:
        return (0, found.group(1), item["rel"])
    return (1, item.get("mtime") or "", item["rel"])


def _persona_label(persona_id: str) -> str:
    if persona_id == "_template":
        return "模版"
    profile = PERSONA_DIR / persona_id / "profile.txt"
    if profile.is_file():
        for line in profile.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("名字：") or line.startswith("名字:"):
                name = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                if name:
                    return name
    return persona_id


def _sorted(items: list[dict]) -> list[dict]:
    return sorted(items, key=_chrono)


def list_readable(only: str | None = None) -> dict:
    """Dock catalog. `only` keeps a single persona; other people stay out of the tree."""
    _ensure()
    from .persona import active_id

    buckets: dict[str, dict[str, list[dict]]] = {}

    def bucket(persona_id: str) -> dict[str, list[dict]]:
        return buckets.setdefault(
            persona_id,
            {"transcript": [], "memory": [], "lore": [], "persona": []},
        )

    if TRANSCRIPT_DIR.exists():
        for path in TRANSCRIPT_DIR.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in READABLE_SUFFIX:
                continue
            if path.suffix.lower() == ".jsonl" and path.with_suffix(".txt").is_file():
                continue
            rel = path.relative_to(TRANSCRIPT_DIR)
            persona_id = rel.parts[0] if len(rel.parts) > 1 else "unknown"
            title = path.stem if _DATE_IN_NAME.search(path.name) else path.name
            bucket(persona_id)["transcript"].append(
                _item(f"transcript/{rel.as_posix()}", path, "transcript", title)
            )

    if PERSONA_DIR.exists():
        for persona_path in PERSONA_DIR.iterdir():
            if not persona_path.is_dir() or persona_path.name.startswith("."):
                continue
            pid = persona_path.name
            for name in MEMORY_FILENAMES:
                path = persona_path / "secondary" / name
                if path.is_file():
                    bucket(pid)["memory"].append(
                        _item(f"memory/{pid}/secondary/{name}", path, "memory", f"secondary/{name}")
                    )
            mem_root = persona_path / "memory"
            if mem_root.is_dir():
                for path in mem_root.rglob("*"):
                    if path.suffix.lower() not in READABLE_SUFFIX or not path.is_file():
                        continue
                    rel = path.relative_to(mem_root).as_posix()
                    bucket(pid)["memory"].append(
                        _item(f"memory/{pid}/{rel}", path, "memory", rel)
                    )
            for name in PERSONA_FILENAMES:
                path = persona_path / name
                if path.is_file():
                    bucket(pid)["persona"].append(
                        _item(f"persona/{pid}/{name}", path, "persona", name)
                    )
            lore_root = persona_path / "lore"
            if lore_root.is_dir():
                for path in lore_root.rglob("*.txt"):
                    if not path.is_file() or path.name == "notes.txt":
                        continue
                    rel = path.relative_to(lore_root).as_posix()
                    bucket(pid)["lore"].append(_item(f"lore/{pid}/{rel}", path, "lore", rel))

    current = active_id()
    section_labels = (
        ("transcript", "对话"),
        ("memory", "记忆"),
        ("lore", "设定"),
        ("persona", "人设"),
    )
    personas = []
    ordered_ids = sorted(
        buckets,
        key=lambda pid: (pid == "_template", pid != current, _persona_label(pid)),
    )
    for pid in ordered_ids:
        sections = []
        for key, label in section_labels:
            files = _sorted(buckets[pid][key])
            if files:
                sections.append({"id": key, "label": label, "files": files})
        if sections:
            personas.append({"id": pid, "name": _persona_label(pid), "sections": sections})
    if only:
        personas = [item for item in personas if item["id"] == only]

    logs = []
    named_logs = (
        (DATA_DIR / "heartbeat.log", "log/heartbeat.log", "心跳"),
        (DATA_DIR / "supervisor.jsonl", "log/supervisor.jsonl", "监督"),
        (KEEPER_LOG, "log/keeper.log", "保存记录"),
    )
    for path, file_id, title in named_logs:
        if path.is_file():
            logs.append(_item(file_id, path, "log", title))

    return {"personas": personas, "logs": logs, "persona": only}


def runtime_view(persona_id: str, kind: str) -> dict:
    """One persona's slice of the shared runtime logs.

    supervisor.jsonl and heartbeat.log are still one file for every persona.
    This view only returns lines that name the requested persona.
    """
    persona_id = (persona_id or "").strip()
    if not persona_id or "/" in persona_id or "\\" in persona_id:
        raise ValueError("无效人格")
    kind = (kind or "").strip()
    if kind == "supervisor":
        path = DATA_DIR / "supervisor.jsonl"
        kept = _filter_supervisor(path, persona_id)
        title = "监督"
    elif kind == "heartbeat":
        path = DATA_DIR / "heartbeat.log"
        kept = _filter_heartbeat(path, persona_id)
        title = "心跳"
    else:
        raise ValueError("无效日志")
    text = "\n".join(kept).strip()
    return {
        "persona": persona_id,
        "kind": kind,
        "title": title,
        "shared": True,
        "content": text or "（这位还没有记录）",
    }


def _filter_supervisor(path: Path, persona_id: str) -> list[str]:
    if not path.is_file():
        return []
    kept: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            if persona_id in line:
                kept.append(line)
            continue
        owner = row.get("persona_id") or (row.get("packet") or {}).get("persona_id")
        if owner == persona_id:
            kept.append(line)
    return kept


def _filter_heartbeat(path: Path, persona_id: str) -> list[str]:
    if not path.is_file():
        return []
    kept: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if f'"persona": "{persona_id}"' in line or f"persona={persona_id}" in line or f'"persona_id": "{persona_id}"' in line:
            kept.append(line)
    return kept


def resolve_readable(file_id: str) -> Path:
    if "/" not in file_id or ".." in file_id or "\\" in file_id:
        raise ValueError("无效文件")
    kind, rest = file_id.split("/", 1)
    if not rest or rest.startswith("/"):
        raise ValueError("无效文件")

    if kind == "log":
        allowed = {
            "keeper.log": KEEPER_LOG,
            "heartbeat.log": DATA_DIR / "heartbeat.log",
            "supervisor.jsonl": DATA_DIR / "supervisor.jsonl",
        }
        if rest not in allowed:
            raise ValueError("无效文件")
        path = allowed[rest]
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
