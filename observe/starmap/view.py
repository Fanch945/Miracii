"""Read-only star-map observer. Not mounted in the chat app.

Open with:  python observe/starmap/view.py
It serves a page on its own port and reads starmap.jsonl. It does not write memory.
"""

from __future__ import annotations

import json
import math
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
PAGE = Path(__file__).with_name("index.html")

import sys

sys.path.insert(0, str(ROOT))

from app import persona, starmap  # noqa: E402

KINDS = ("lore", "semantic", "impression", "episodic")


def _names(persona_id: str) -> list[str]:
    profile = (persona.folder(persona_id) / "profile.txt").read_text(encoding="utf-8", errors="replace")
    found = {"名字": persona_id, "对方": "对方"}
    for line in profile.splitlines():
        for key in found:
            if line.startswith(key + "：") or line.startswith(key + ":"):
                value = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                if value:
                    found[key] = value
    return [found["名字"], found["对方"]]


def _label(body: str) -> str:
    for line in (body or "").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("keys:") or text.startswith("---"):
            continue
        return text[:42]
    return "（空）"


def _bodies(persona_id: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for star in starmap.blocks(persona_id):
        path = persona.folder(persona_id) / star["file"]
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if star["kind"] == "semantic":
            lines = [
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.strip().startswith("#") and "|" in line
            ]
            if 0 <= star["block"] < len(lines):
                out[star["fp"]] = lines[star["block"]]
            continue
        chunks = [c.strip() for c in starmap.BLOCK_SPLIT.split(text) if c.strip()]
        if not chunks and text.strip():
            chunks = [text.strip()]
        if 0 <= star["block"] < len(chunks):
            out[star["fp"]] = chunks[star["block"]]
    return out


def _days(created: str | None, today: date) -> int | None:
    if not created:
        return None
    try:
        born = datetime.strptime(created, "%Y-%m-%d").date()
    except ValueError:
        return None
    return max(0, (today - born).days)


def sky(persona_id: str) -> dict:
    today = date.today()
    bodies = _bodies(persona_id)
    stars = []
    for star in starmap.project(persona_id, today=today):
        kind = star.get("kind") or "lore"
        if kind not in KINDS:
            continue
        sector = KINDS.index(kind)
        age = _days(star.get("created"), today)
        radius = 0.9 if age is None else 0.22 + 0.62 * min(1.0, age / 180)
        stars.append(
            {
                "id": f"{star['file']}#{star['block']}",
                "fp": star["fp"],
                "kind": kind,
                "level": star.get("level"),
                "ew": star.get("ew"),
                "rc": star.get("rc") or 0,
                "created": star.get("created"),
                "file": star["file"],
                "block": star["block"],
                "brightness": star.get("brightness"),
                "mag": star.get("mag"),
                "undated": star.get("brightness") is None,
                "label": _label(bodies.get(star["fp"], "")),
                "sector": sector,
                "radius": radius,
            }
        )
    buckets: dict[tuple[int, float], list[dict]] = {}
    for star in stars:
        buckets.setdefault((star["sector"], round(star["radius"], 2)), []).append(star)
    for group in buckets.values():
        span = math.pi / 2 * 0.82
        start = group[0]["sector"] * (math.pi / 2) - span / 2
        step = span / max(1, len(group))
        for index, star in enumerate(group):
            angle = start + step * (index + 0.5)
            star["x"] = math.cos(angle) * star["radius"]
            star["y"] = math.sin(angle) * star["radius"]
    links = []
    by_fp: dict[str, list[dict]] = {}
    by_day: dict[str, list[dict]] = {}
    for star in stars:
        by_fp.setdefault(star["fp"], []).append(star)
        if star.get("created"):
            by_day.setdefault(star["created"], []).append(star)
    for group in by_fp.values():
        if len(group) < 2:
            continue
        for other in group[1:]:
            links.append({"a": group[0]["id"], "b": other["id"], "kind": "same"})
    for group in by_day.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: item["id"])
        for left, right in zip(ordered, ordered[1:]):
            if left["fp"] == right["fp"]:
                continue
            links.append({"a": left["id"], "b": right["id"], "kind": "day"})
    for star in stars:
        star.pop("sector", None)
        star.pop("radius", None)
    return {
        "persona": persona_id,
        "today": today.isoformat(),
        "core": _names(persona_id),
        "stars": stars,
        "links": links,
    }


def serve(persona_id: str, port: int, open_browser: bool) -> None:
    payload = sky(persona_id)
    page = PAGE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/sky":
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path in {"/", "/index.html"}:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            self.send_error(404)

        def log_message(self, fmt: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"星图观察 {persona_id}  {url}  {len(payload['stars'])} 颗")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="观察用星图，不接入聊天应用")
    parser.add_argument("--persona", default=None)
    parser.add_argument("--port", type=int, default=7811)
    parser.add_argument("--no-open", action="store_true")
    options = parser.parse_args()
    serve(options.persona or persona.active_id(), options.port, not options.no_open)
