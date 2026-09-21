from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path

from .files import DATA_DIR

MEDIA_DIR = DATA_DIR / "media"
IMAGE_MARK = re.compile(r"^\[图:(.+)\]$")
ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
MAX_IMAGES = 3
MAX_BYTES = 4 * 1024 * 1024
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def split_content(content: str) -> tuple[str, list[str]]:
    text_lines: list[str] = []
    images: list[str] = []
    for line in (content or "").splitlines():
        match = IMAGE_MARK.match(line.strip())
        if match:
            images.append(match.group(1).strip())
        else:
            text_lines.append(line)
    return "\n".join(text_lines).strip(), images


def join_content(text: str, image_rels: list[str]) -> str:
    parts = [text.strip()] if text.strip() else []
    for rel in image_rels:
        parts.append(f"[图:{rel}]")
    return "\n".join(parts) if parts else "（看）"


def public_url(rel: str) -> str:
    return "/api/media/" + rel.lstrip("/")


def resolve(rel: str) -> Path:
    if ".." in rel or rel.startswith("/") or "\\" in rel:
        raise ValueError("无效图片")
    parts = rel.split("/")
    if len(parts) != 2 or not all(SAFE_NAME.match(part) for part in parts):
        raise ValueError("无效图片")
    path = (MEDIA_DIR / parts[0] / parts[1]).resolve()
    if not path.is_file() or not _under(path, MEDIA_DIR):
        raise FileNotFoundError("图片不存在")
    return path


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def save_images(persona_id: str, images: list[dict]) -> list[dict]:
    """Persist uploaded images. Each item: {mime, data} where data is raw base64."""
    if len(images) > MAX_IMAGES:
        raise ValueError(f"一次最多 {MAX_IMAGES} 张图")
    saved: list[dict] = []
    folder = MEDIA_DIR / persona_id
    folder.mkdir(parents=True, exist_ok=True)
    for item in images:
        mime = str(item.get("mime") or "").lower().split(";")[0].strip()
        ext = ALLOWED_MIME.get(mime)
        if not ext:
            raise ValueError("只支持 JPEG / PNG / GIF / WebP")
        raw_b64 = str(item.get("data") or "")
        if "," in raw_b64 and raw_b64.strip().startswith("data:"):
            raw_b64 = raw_b64.split(",", 1)[1]
        try:
            blob = base64.b64decode(raw_b64, validate=False)
        except Exception as exc:
            raise ValueError("图片编码无效") from exc
        if not blob:
            raise ValueError("空图片")
        if len(blob) > MAX_BYTES:
            raise ValueError("单张图不要超过 4MB，请压缩后再发")
        name = uuid.uuid4().hex[:12] + ext
        path = folder / name
        path.write_bytes(blob)
        rel = f"{persona_id}/{name}"
        data_url = f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"
        saved.append({"rel": rel, "url": public_url(rel), "data_url": data_url, "mime": mime})
    return saved


def data_url_for(rel: str) -> str | None:
    try:
        path = resolve(rel)
    except (ValueError, FileNotFoundError):
        return None
    ext = path.suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(
        ext, "image/jpeg"
    )
    blob = path.read_bytes()
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"
