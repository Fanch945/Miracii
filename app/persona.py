from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PERSONA_DIR = ROOT / "persona"
ACTIVE_FILE = PERSONA_DIR / "active.txt"
MAIN_ID = "miracii"
RESERVED = {"_template"}


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def active_id() -> str:
    value = _read(ACTIVE_FILE).splitlines()[0].strip() if ACTIVE_FILE.exists() else ""
    return value or MAIN_ID


def set_active(persona_id: str) -> str:
    persona_id = persona_id.strip()
    target = PERSONA_DIR / persona_id
    if persona_id in RESERVED or not target.is_dir() or not (target / "profile.txt").exists():
        raise ValueError(f"没有这个人格：{persona_id}")
    ACTIVE_FILE.write_text(persona_id + "\n", encoding="utf-8")
    return persona_id


def folder(persona_id: str | None = None) -> Path:
    return PERSONA_DIR / (persona_id or active_id())


def list_personas() -> list[dict]:
    current = active_id()
    items: list[dict] = []
    for path in sorted(PERSONA_DIR.iterdir()):
        if not path.is_dir() or path.name in RESERVED:
            continue
        if not (path / "profile.txt").exists():
            continue
        items.append(
            {
                "id": path.name,
                "name": display_name(path.name),
                "active": path.name == current,
                "main": path.name == MAIN_ID,
            }
        )
    return items


def display_name(persona_id: str | None = None) -> str:
    profile = _read(folder(persona_id) / "profile.txt")
    for line in profile.splitlines():
        if line.startswith("名字：") or line.startswith("名字:"):
            return line.split("：", 1)[-1].split(":", 1)[-1].strip() or (persona_id or MAIN_ID)
    return persona_id or MAIN_ID


def secondary_text(persona_id: str | None = None) -> str:
    return _read(folder(persona_id) / "secondary" / "pending.txt")


def always_lore(persona_id: str | None = None) -> str:
    return _read(folder(persona_id) / "lore" / "always.txt")


def working_memory(persona_id: str | None = None) -> str:
    return _read(folder(persona_id) / "memory" / "working.txt")


def system_prompt(persona_id: str | None = None, scan_text: str = "") -> str:
    from .lore import format_for_prompt, select_entries

    base = folder(persona_id)
    profile = _read(base / "profile.txt")
    voice = _read(base / "voice_lines.txt")
    forbidden = _read(base / "forbidden_style.txt")
    examples = _read(base / "examples.txt")
    always = always_lore(persona_id)
    working = working_memory(persona_id)
    extra = secondary_text(persona_id)
    recalled = format_for_prompt(select_entries(persona_id, scan_text))
    from .supervisor import parse_schedule
    from .when import prompt_block, wanted

    need = wanted(scan_text)
    clock = ""
    if need["time"] or need["calendar"]:
        tz_name = str(parse_schedule(persona_id).get("timezone") or "Asia/Shanghai")
        clock = prompt_block(
            persona_id,
            tz_name=tz_name,
            want_time=need["time"] or need["calendar"],
            want_calendar=need["calendar"],
            scan_text=scan_text,
        )

    parts = [
        "下面是你的本地人设。按这个人说话，不要变成通用助手。",
        "",
        "【人设】",
        profile,
    ]
    if always:
        parts.extend(["", "【常驻设定】", always])
    if working:
        parts.extend(["", "【短期工作记忆】", working])
    if recalled:
        parts.extend(["", recalled])
    parts.extend(
        [
            "",
            "【原话样本，跟这个分布，不要解释人设】",
            voice,
            "",
            "【禁止的句式】",
            forbidden,
            "",
            "【对话示范】",
            examples,
        ]
    )
    if extra:
        parts.extend(["", "【次要人设 / 尚未并入主档案的成长条目】", extra])
    if clock:
        parts.extend(["", clock])
    parts.extend(
        [
            "",
            "只输出你要对对方说的那一句或几句。不要输出内心独白、分析、Markdown 标题或编号列表。",
            "长篇背景、复杂人际关系、履历技能若对方没提起，不要主动讲成自我介绍。",
        ]
    )
    return "\n".join(parts).strip()
