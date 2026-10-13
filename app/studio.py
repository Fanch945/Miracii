"""Persona studio: explicit save, import, and export. No automatic writes."""

from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path

from . import persona

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ALLOWED = {".txt", ".jsonl"}
_MAX_ZIP = 8_000_000


def check_id(persona_id: str) -> str:
    persona_id = (persona_id or "").strip()
    if persona_id in persona.RESERVED or not _ID.match(persona_id):
        raise ValueError("人格 id 只能用字母、数字、短横线，不能用模版目录")
    return persona_id


def persona_dir(persona_id: str) -> Path:
    persona_id = check_id(persona_id)
    root = (persona.PERSONA_DIR / persona_id).resolve()
    root.relative_to(persona.PERSONA_DIR.resolve())
    return root


def _file(persona_id: str, rel: str) -> Path:
    rel = (rel or "").replace("\\", "/").strip().lstrip("/")
    if not rel or any(part in {"", ".", ".."} for part in rel.split("/")):
        raise ValueError("无效文件")
    path = (persona_dir(persona_id) / rel).resolve()
    path.relative_to(persona_dir(persona_id))
    if path.suffix.lower() not in _ALLOWED:
        raise ValueError("只能编辑 txt 或 jsonl")
    return path


def _group(rel: str) -> str:
    if rel.startswith("lore/"):
        return "设定"
    if rel.startswith("memory/"):
        return "记忆"
    if rel.startswith("secondary/"):
        return "次要"
    return "人设"


def list_files(persona_id: str) -> list[dict]:
    root = persona_dir(persona_id)
    if not root.is_dir():
        raise ValueError("没有这个人格")
    items = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _ALLOWED:
            continue
        rel = path.relative_to(root).as_posix()
        items.append({"path": rel, "group": _group(rel), "name": path.name})
    return items


def read_file(persona_id: str, rel: str) -> str:
    path = _file(persona_id, rel)
    if not path.is_file():
        raise FileNotFoundError("文件不存在")
    return path.read_text(encoding="utf-8")


def write_file(persona_id: str, rel: str, content: str) -> None:
    from .starmap import INDEX_NAME

    path = _file(persona_id, rel)
    if not path.is_file():
        raise FileNotFoundError("文件不存在，先新建")
    if path.name == INDEX_NAME:
        raise ValueError(f"{INDEX_NAME} 是星图索引，由程序维护，不要在设定页改它")
    path.write_text(content, encoding="utf-8")


def create_file(persona_id: str, rel: str) -> None:
    path = _file(persona_id, rel)
    if path.exists():
        raise ValueError("文件已存在")
    path.parent.mkdir(parents=True, exist_ok=True)
    if "stories/" in rel.replace("\\", "/"):
        body = "keys:\nalso:\nform: story\n---\n"
    elif rel.replace("\\", "/").startswith("lore/entries/"):
        body = "keys:\nconstant: no\nform: digest\n---\n"
    else:
        body = ""
    path.write_text(body, encoding="utf-8")


def create_persona(persona_id: str) -> str:
    persona_id = check_id(persona_id)
    dest = persona_dir(persona_id)
    if dest.exists():
        raise ValueError("已经有这个人格")
    src = persona.PERSONA_DIR / "_template"
    if not src.is_dir():
        raise ValueError("缺少人格模版")
    shutil.copytree(src, dest)
    return persona_id


def export_zip(persona_id: str) -> bytes:
    root = persona_dir(persona_id)
    if not root.is_dir():
        raise ValueError("没有这个人格")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _ALLOWED:
                continue
            rel = path.relative_to(root).as_posix()
            archive.write(path, f"{persona_id}/{rel}")
    return buffer.getvalue()


def import_zip(raw: bytes, persona_id: str, *, replace: bool = False) -> str:
    if len(raw) > _MAX_ZIP:
        raise ValueError("压缩包太大")
    persona_id = check_id(persona_id)
    dest = persona_dir(persona_id)
    if dest.exists() and not replace:
        raise ValueError("已经有这个人格。导入覆盖需要确认")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("不是有效的压缩包") from exc
    names = []
    for original in archive.namelist():
        name = original.replace("\\", "/")
        if name.endswith("/"):
            continue
        names.append((original, name))
    if not names:
        raise ValueError("压缩包是空的")
    roots = {name.split("/", 1)[0] for _, name in names if "/" in name}
    prefix = ""
    if len(roots) == 1 and all("/" in name for _, name in names):
        prefix = next(iter(roots)) + "/"
    planned: list[tuple[str, bytes]] = []
    for original, name in names:
        rel = name[len(prefix) :] if prefix and name.startswith(prefix) else name
        rel = rel.strip("/")
        if not rel or any(part in {"", ".", ".."} for part in rel.split("/")):
            raise ValueError("压缩包路径无效")
        if Path(rel).suffix.lower() not in _ALLOWED:
            continue
        planned.append((rel, archive.read(original)))
    if not any(rel == "profile.txt" for rel, _ in planned):
        raise ValueError("压缩包里需要有 profile.txt")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for rel, data in planned:
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return persona_id
