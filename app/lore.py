from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP_NAMES = {"notes.txt"}
HEADER_END = "---"
_KEY_SPLIT = re.compile(r"[,，、;；]")

# 所有细稿共用：对方在要「那一场」，而不是要词条。
# 单靠这些不够指定哪一篇；还要 keys 对上话题，或多篇时靠 also 里的独有情节词拆开。
STORY_INTENT = (
    "你还记得",
    "还记得",
    "那次",
    "那一回",
    "那一晚",
    "那一天",
    "那件事",
    "怎么发生",
    "经过是",
)


@dataclass
class LoreHit:
    path: Path
    form: str
    keys_hit: list[str]
    also_hit: list[str]
    intent_hit: list[str]
    body: str
    spec: int = 0


@dataclass
class LoreEntry:
    path: Path
    keys: list[str] = field(default_factory=list)
    also: list[str] = field(default_factory=list)
    form: str = "digest"
    constant: bool = False
    body: str = ""


def load_entries(persona_id: str | None = None) -> list[LoreEntry]:
    from .persona import folder

    root = folder(persona_id) / "lore" / "entries"
    if not root.is_dir():
        return []
    items: list[LoreEntry] = []
    for path in sorted(root.rglob("*.txt")):
        if not path.is_file() or path.name in SKIP_NAMES:
            continue
        parsed = _parse(path)
        if parsed and parsed.body:
            items.append(parsed)
    return items


def select_entries(persona_id: str | None, scan_text: str, *, max_digest: int = 3, max_story: int = 1) -> list[LoreHit]:
    text = (scan_text or "").casefold()
    if not text.strip():
        return []
    hits = [hit for entry in load_entries(persona_id) if (hit := match_entry(entry, text))]
    digests = [h for h in hits if h.form != "story"]
    digests.sort(key=lambda h: h.spec, reverse=True)
    stories = _pick_stories([h for h in hits if h.form == "story"])
    chosen: list[LoreHit] = []
    chosen.extend(_budget(digests, max_digest, 1600))
    chosen.extend(_budget(stories, max_story, 1800))
    return chosen


def format_for_prompt(hits: list[LoreHit]) -> str:
    if not hits:
        return ""
    parts: list[str] = []
    digests = [h for h in hits if h.form != "story"]
    stories = [h for h in hits if h.form == "story"]
    if digests:
        parts.append("【设定词条】")
        parts.append("下面是现在用得上的事实。当作已知，不要主动讲成自我介绍。")
        for hit in digests:
            parts.append(hit.body)
    if stories:
        parts.append("【回忆场面】")
        parts.append("对方在问那一次怎么发生。按这场说，但不要把无关的整段经历倒出来。")
        for hit in stories:
            parts.append(hit.body)
    return "\n".join(parts).strip()


def match_entry(entry: LoreEntry, text: str) -> LoreHit | None:
    if entry.constant:
        return LoreHit(entry.path, entry.form, [], [], [], entry.body, spec=0)
    primary = [k for k in entry.keys if k.casefold() in text]
    unique = [k for k in entry.also if k.casefold() in text]
    intent = [k for k in STORY_INTENT if k.casefold() in text]
    if entry.form == "story":
        if unique:
            spec = max(len(k) for k in unique)
            return LoreHit(entry.path, "story", primary, unique, intent, entry.body, spec=spec)
        if primary and intent:
            spec = max(len(k) for k in primary)
            return LoreHit(entry.path, "story", primary, [], intent, entry.body, spec=spec)
        return None
    if not primary:
        return None
    spec = max(len(k) for k in primary)
    return LoreHit(entry.path, "digest", primary, [], [], entry.body, spec=spec)


def _pick_stories(hits: list[LoreHit]) -> list[LoreHit]:
    if not hits:
        return []
    unique = [h for h in hits if h.also_hit]
    if unique:
        unique.sort(key=lambda h: h.spec, reverse=True)
        return unique[:1]
    # 只有「还记得 + 小时候」这类话题+意图：多篇同时命中就都不注入，只留词条。
    if len(hits) == 1:
        return hits
    return []


def _parse(path: Path) -> LoreEntry | None:
    raw = path.read_text(encoding="utf-8")
    header, sep, rest = raw.partition(HEADER_END)
    if not sep:
        return None
    fields: dict[str, str] = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        fields[name.strip().casefold()] = value.strip()
    form = (fields.get("form") or "digest").casefold()
    if form not in {"digest", "story"}:
        form = "digest"
    return LoreEntry(
        path=path,
        keys=_split_keys(fields.get("keys") or ""),
        also=_split_keys(fields.get("also") or fields.get("secondary") or ""),
        form=form,
        constant=_truthy(fields.get("constant")),
        body=rest.strip(),
    )


def _split_keys(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for chunk in _KEY_SPLIT.split(raw):
        key = chunk.strip()
        if len(key) < 2 or key.startswith("（") or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().casefold() in {"yes", "true", "1", "y"}


def _budget(hits: list[LoreHit], limit: int, char_cap: int) -> list[LoreHit]:
    chosen: list[LoreHit] = []
    used = 0
    for hit in hits:
        if len(chosen) >= limit:
            break
        size = len(hit.body)
        if chosen and used + size > char_cap:
            continue
        chosen.append(hit)
        used += size
    return chosen
