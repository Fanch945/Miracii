"""积温：确定性的主动意识引擎（通用层）。

借 `ClaraShafiq/jiwen`（MIT，本地克隆在 `D:\\SeelAI\\ClaraShafiq\\jiwen`）的思路重写，不是移植：
- 保留它最值钱的三件事：**确定性替代随机**、**数值翻成人话再进 prompt**、**每个闸门报"差多少"**；
- 通用层只放五个核心轴与一条声明式的特化槽位，人格特化不写代码；
- 加入两条它没有的：**hesitation（犹豫）轴**与 **user_state 调制**。

它不做回复生成、不落盘、不调模型：`tick()` 只推进状态并返回触发动作。

与参照实现的差异（都在《docs/reference/jiwen.txt》记过）：
- `immersion` 在那边是角色特化的轴，这里降为通用轴，特化交给 `extra_axes`；
- 阈值先继承它调参后的起点（见 `defaults()`），不是它的出厂值。

改记忆？不。这个模块只读时间与调用方给的消息时间戳，永远不碰记忆文件。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

# ── 通用轴的取值范围（与参照实现一致）──
AXIS_RANGES: dict[str, tuple[float, float]] = {
    "connection": (0.0, 1.0),
    "pride": (-1.0, 1.0),
    "valence": (-1.0, 1.0),
    "arousal": (-1.0, 1.0),
    "immersion": (0.0, 1.0),
    "hesitation": (0.0, 1.0),
}

MAX_TICK_MINUTES = 60.0
# 参照实现里写死的两个 immersion 边界（jiwen.js:462-467、507-516）
BUFFER_IMMERSION = 0.2  # 骄傲挡住开口时，沉浸低于此值才转而找事做
REGULATE_IMMERSION = 0.3  # 自我调节也要求没那么沉浸


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class Thresholds:
    """触发闸门。默认值继承参照实现**调参后**的起点，不是出厂值。"""

    observation: float = 0.20
    consider_contact: float = 0.35
    force_contact: float = 0.50
    pride_block: float = 0.50
    valence_activity: float = -1.0  # -1.0 = 永不
    arousal_agitation: float = 0.7
    # 我们加的：犹豫把开口门槛往上抬多少（0 = 犹豫不影响）
    hesitation_lift: float = 0.10


@dataclass(frozen=True)
class Rates:
    """每分钟的漂移速率。名字与默认值对齐参照实现，单位一致。"""

    connection_base: float = 0.0007
    connection_accel: float = 1.0  # 她调参后：1.5 → 1.0
    accel_delay: float = 45.0  # 她调参后：30 → 45
    immersion_decay: float = 0.010
    immersion_dampen_connection: float = 1.0
    pride_regress: float = 0.003
    # 0.35 = 一旦进到"有点想找对方"的带里，骄傲就开始防御。
    # 参照实现的出厂值是 1.0（永不防御），但那是**关掉**这个机制，不是调过参——
    # 而"嘴硬、拉不下脸、转而找事做"正是这套设计最有性格的部分。
    pride_defend_threshold: float = 0.35
    pride_defend_target: float = 0.5
    pride_defend_rate: float = 0.003
    valence_regress: float = 0.005
    valence_setpoint: float = 0.0
    valence_lock_threshold: float = 1.0  # 1.0 = 永不
    valence_lock_factor: float = 0.15  # 她调过：0.15 = 减慢 85%
    # 等待下沉必须强过回归，否则这条轴永远只是摆设（见 docs/changes/2026-10-14-warmth-fixes.txt）
    valence_connection_drift_threshold: float = 0.0
    valence_connection_drift_rate: float = 0.012
    # Valence → connection 增长倍率：轻度不开心时想要安慰，严重低落时自我封闭。
    # 与参照一致，默认关闭；要用就在 warmth.json 里打开。
    valence_connect_boost: float = 0.0
    valence_connect_boost_threshold: float = -0.2
    valence_connect_dampen: float = 0.0
    valence_connect_dampen_threshold: float = -0.4
    arousal_regress: float = 0.005
    arousal_setpoint: float = 0.0
    arousal_connection_rise_threshold: float = 0.30
    # 同样必须强过回归：0.0075 > 0.005，等待约五小时后才会真的坐不住
    arousal_connection_rise_rate: float = 0.0075
    arousal_lock_threshold: float = 1.0
    arousal_lock_factor: float = 0.3
    pride_arousal_conflict_rate: float = 0.002
    # 我们加的：犹豫的涨落（只要"想找他"且"端着"就一直在咽，没有上界）
    hesitation_rise: float = 0.004
    hesitation_decay: float = 0.003
    hesitation_source_floor: float = 0.35  # connection 到这个带才谈得上犹豫


@dataclass(frozen=True)
class AxisSpec:
    """人格特化轴：声明式，不写代码。

    - `regress` 型：向 `setpoint` 回归；
    - `driven` 型：当 `source` 轴越过 `threshold` 时按 `rate` 推进（可负）。
    """

    name: str
    kind: str = "regress"
    initial: float = 0.0
    low: float = -1.0
    high: float = 1.0
    setpoint: float = 0.0
    rate: float = 0.0
    source: str = "connection"
    threshold: float = 1.0


_AXIS_NUMERIC = ("initial", "low", "high", "setpoint", "rate", "threshold")


@dataclass
class Config:
    thresholds: Thresholds = field(default_factory=Thresholds)
    rates: Rates = field(default_factory=Rates)
    extra_axes: tuple[AxisSpec, ...] = ()
    # user_state → 开口门槛的乘数（>1 = 更克制）。参照实现把 user_state 留给外部 LLM 填。
    user_state_lift: dict[str, float] = field(
        default_factory=lambda: {"active": 1.0, "busy": 1.35, "away": 1.1, "sleeping": 2.0}
    )
    # 两次主动开口之间的最短间隔（分钟）。0 = 不限制。
    speak_cooldown_minutes: float = 45.0
    # 单次空闲 tick 最多按多少分钟推进。停机一夜不会一次性补满——
    # 她并没有在关机时"等着"，一开机就扑上来也不像她。改这个值就能改这个取舍。
    max_tick_minutes: float = MAX_TICK_MINUTES
    # 对方回话时的一次性回落：等待应当由"你出现了"解除，
    # 而不是她自己开了口就自我安慰（那样她会一路说下去，直到配额拦停）。
    # 数值是暂定的：真正该多大要等标注数据（见 docs/changes/2026-10-14-warmth-fixes.txt）。
    reply_delta: dict[str, float] = field(
        default_factory=lambda: {
            "connection": -0.25,
            "valence": 0.05,
            "arousal": -0.15,
            "hesitation": -0.20,
        }
    )
    persona: dict[str, str] = field(
        default_factory=lambda: {"subject_name": "对方", "self_name": "你", "pronoun": "ta"}
    )


def defaults() -> Config:
    """通用默认：任何角色拿来就能跑。"""
    return Config()


CONFIG_NAME = "warmth.json"


def template() -> dict:
    """写给人填的特化模板（`warmth.json` 的形状）。

    **故意只写值得调的旋钮，不是全部。** 早先这里把三十来个速率全列上，
    结果文件一生成就把默认值**钉死**了——以后改默认值再也影响不到这个人格，
    而且还留下了漂移（引擎删掉的字段仍留在文件里被报"不认识"）。
    想看当前生效的全部旋钮用 `--show`，那才是全量。

    键名与 `Config` / `Thresholds` / `Rates` 的字段名一一对应；`_` 开头的键只是说明，加载时忽略。
    """
    return {
        "_说明": "积温的人格特化旋钮。只列值得改的；删掉任何一项都会退回默认值，整个文件删掉也没关系。"
        "想看全部生效值：python -m app.warmth --show",
        "_示例_特化轴": [
            {
                "name": "执念",
                "kind": "driven",
                "initial": 0.0,
                "low": 0.0,
                "high": 1.0,
                "source": "connection",
                "threshold": 0.3,
                "rate": 0.001,
            }
        ],
        "thresholds": {
            "observation": 0.20,
            "consider_contact": 0.35,
            "force_contact": 0.50,
            "pride_block": 0.50,
            "valence_activity": -1.0,
            "arousal_agitation": 0.7,
            "hesitation_lift": 0.10,
        },
        "rates": {
            "connection_base": 0.0007,
            "connection_accel": 1.0,
            "accel_delay": 45.0,
            "pride_defend_threshold": 0.35,
            "pride_defend_target": 0.5,
            "pride_defend_rate": 0.003,
            "valence_connection_drift_rate": 0.012,
            "arousal_connection_rise_threshold": 0.30,
            "arousal_connection_rise_rate": 0.0075,
            "hesitation_rise": 0.004,
            "hesitation_decay": 0.003,
        },
        "user_state_lift": {"active": 1.0, "busy": 1.35, "away": 1.1, "sleeping": 2.0},
        "speak_cooldown_minutes": 45.0,
        "max_tick_minutes": 60.0,
        "reply_delta": {"connection": -0.25, "valence": 0.05, "arousal": -0.15, "hesitation": -0.20},
        "extra_axes": [],
    }


def config_path(persona_id: str | None = None) -> Path:
    from . import persona

    return persona.folder(persona_id) / "memory" / CONFIG_NAME


def _coerce_fields(target, raw: dict, *, strict: bool = True) -> tuple[object, list[str]]:
    """只认 dataclass 上已有的字段，并按原字段的类型转换。坏值跳过、退回默认。"""
    if not isinstance(raw, dict):
        return target, [f"{type(target).__name__}: 不是对象，已忽略"]
    notes: list[str] = []
    values = {}
    for key, value in raw.items():
        if key.startswith("_") or not hasattr(target, key):
            if not key.startswith("_"):
                notes.append(f"{type(target).__name__}.{key}: 不认识，已忽略")
            continue
        current = getattr(target, key)
        if isinstance(current, bool):
            values[key] = bool(value)
        elif isinstance(current, int) and not isinstance(value, bool):
            try:
                values[key] = int(value)
            except (TypeError, ValueError):
                notes.append(f"{type(target).__name__}.{key}: {value!r} 不是数字，用默认")
        elif isinstance(current, float):
            try:
                number = float(value)
            except (TypeError, ValueError):
                notes.append(f"{type(target).__name__}.{key}: {value!r} 不是数字，用默认")
            else:
                values[key] = number if math.isfinite(number) else current
        else:
            values[key] = value
    return replace(target, **values), notes


def load_config(persona_id: str | None = None, *, path: Path | None = None) -> tuple[Config, list[str]]:
    """读人格特化配置。缺文件、坏 JSON、坏值一律退回默认——配置问题不该让引擎起不来。

    返回 `(config, notes)`；`notes` 列出被忽略的键与坏值，供命令行显示。
    """
    target = path or config_path(persona_id)
    base = Config()
    if not target.is_file():
        return base, []
    try:
        # utf-8-sig：Windows 上用记事本 / PowerShell 存盘很容易带 BOM，那是无害的
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return base, [f"{target.name}: 读不了（{exc}），已用默认"]
    if not isinstance(raw, dict):
        return base, [f"{target.name}: 顶层不是对象，已用默认"]
    notes: list[str] = []

    # 顶层键打错字（"rate" 少了 s）会静默失效——宁可吵一声
    known = {
        "thresholds",
        "rates",
        "user_state_lift",
        "extra_axes",
        "persona",
        "speak_cooldown_minutes",
        "max_tick_minutes",
        "reply_delta",
    }
    notes += [
        f"{key}: 顶层键不认识，已忽略" for key in raw if key not in known and not str(key).startswith("_")
    ]

    thresholds, extra = _coerce_fields(base.thresholds, raw.get("thresholds", {}))
    rates, extra_rates = _coerce_fields(base.rates, raw.get("rates", {}))
    notes += extra + extra_rates

    def _positive(key: str, default: float, *, allow_zero: bool = False) -> float:
        raw_value = raw.get(key)
        if raw_value is None:
            return default
        try:
            number = float(raw_value)
        except (TypeError, ValueError):
            notes.append(f"{key}: {raw_value!r} 不是数字，用默认")
            return default
        if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
            notes.append(f"{key}: {raw_value!r} 不是正数，用默认")
            return default
        return number

    cooldown = _positive("speak_cooldown_minutes", base.speak_cooldown_minutes, allow_zero=True)
    # 复核抓到：字段加进 Config 却没接进 load_config，等于"可配"是空话
    max_tick = _positive("max_tick_minutes", base.max_tick_minutes)

    reply = raw.get("reply_delta")
    if isinstance(reply, dict) and reply:
        clean_reply = {
            str(k): float(v)
            for k, v in reply.items()
            if isinstance(v, (int, float)) and math.isfinite(float(v))
        }
        if len(clean_reply) != len(reply):
            notes.append("reply_delta: 有坏值被丢弃")
        reply = clean_reply or base.reply_delta
    else:
        reply = base.reply_delta

    lifts = raw.get("user_state_lift")
    if isinstance(lifts, dict) and lifts:
        clean = {str(k): float(v) for k, v in lifts.items() if isinstance(v, (int, float)) and math.isfinite(float(v))}
        if len(clean) != len(lifts):
            notes.append("user_state_lift: 有坏值被丢弃")
        lifts = clean or base.user_state_lift
    else:
        lifts = base.user_state_lift

    specs: list[AxisSpec] = []
    for index, item in enumerate(raw.get("extra_axes") or []):
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            notes.append(f"extra_axes[{index}]: 缺 name，已忽略")
            continue
        allowed = {f for f in AxisSpec.__dataclass_fields__}
        fields = {k: v for k, v in item.items() if k in allowed}
        bad = None
        for key in _AXIS_NUMERIC:
            if key not in fields:
                continue
            try:
                number = float(fields[key])
            except (TypeError, ValueError):
                bad = f"{key}={fields[key]!r} 不是数字"
                break
            if not math.isfinite(number):
                bad = f"{key} 不是有限值"
                break
            fields[key] = number
        if bad:
            # 类型不查的话，字符串会一路 load 成功，直到 tick 里做乘法才炸
            notes.append(f"extra_axes[{index}]: {bad}，已忽略这条轴")
            continue
        if str(fields.get("kind", "regress")) not in ("regress", "driven"):
            notes.append(f"extra_axes[{index}]: kind={fields.get('kind')!r} 不认识，按 regress 处理")
            fields["kind"] = "regress"
        fields["source"] = str(fields.get("source", "connection"))
        try:
            specs.append(AxisSpec(**fields))
        except TypeError as exc:
            notes.append(f"extra_axes[{index}]: {exc}，已忽略")

    persona_raw = raw.get("persona")
    if persona_raw is None:
        persona = base.persona
    elif isinstance(persona_raw, dict):
        persona = {**base.persona, **{str(k): str(v) for k, v in persona_raw.items()}}
    else:
        # {**base.persona, **"miracii"} 会直接 TypeError——配置坏掉不该让引擎起不来
        notes.append(f"persona: 不是对象（{type(persona_raw).__name__}），已用默认")
        persona = base.persona

    config = Config(
        thresholds=thresholds,
        rates=rates,
        extra_axes=tuple(specs),
        user_state_lift=lifts,
        speak_cooldown_minutes=cooldown,
        max_tick_minutes=max_tick,
        reply_delta=reply,
        persona=persona,
    )
    return config, notes


def write_template(persona_id: str | None = None, *, path: Path | None = None, overwrite: bool = False) -> Path:
    """把模板写到 `persona/<id>/memory/warmth.json`；已存在则不覆盖（除非 overwrite）。"""
    target = path or config_path(persona_id)
    if target.exists() and not overwrite:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(template(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def effective_config(config: Config) -> dict:
    """当前生效的全部旋钮——命令行显示用。"""
    out = {
        "thresholds": {f: getattr(config.thresholds, f) for f in config.thresholds.__dataclass_fields__},
        "rates": {f: getattr(config.rates, f) for f in config.rates.__dataclass_fields__},
        "user_state_lift": dict(config.user_state_lift),
        "speak_cooldown_minutes": config.speak_cooldown_minutes,
        "max_tick_minutes": config.max_tick_minutes,
        "reply_delta": dict(config.reply_delta),
        "persona": dict(config.persona),
        "extra_axes": [
            {f: getattr(spec, f) for f in spec.__dataclass_fields__} for spec in config.extra_axes
        ],
    }
    return out


# ── 状态落盘 ────────────────────────────────────────
STATE_NAME = "warmth-state.json"
_AXIS_FIELDS = ("connection", "pride", "valence", "arousal", "immersion", "hesitation")


def state_path(persona_id: str | None = None) -> Path:
    from . import persona

    return persona.folder(persona_id) / "memory" / STATE_NAME


def state_to_dict(state: State) -> dict:
    return {
        "version": 1,
        **{name: state.axis(name) for name in _AXIS_FIELDS},
        "extra": dict(state.extra),
        "last_activity_at": state.last_activity_at.isoformat() if state.last_activity_at else None,
        "last_tick": state.last_tick.isoformat() if state.last_tick else None,
        "last_spoke_at": state.last_spoke_at.isoformat() if state.last_spoke_at else None,
        "last_gates": list(state.last_gates),
        "user_state": state.user_state,
    }


def state_from_dict(payload: dict) -> State:
    """坏值一律退回该轴的默认，绝不抛异常——状态文件坏了不该让心跳停摆。"""
    state = State()
    if not isinstance(payload, dict):
        return state
    for name in _AXIS_FIELDS:
        if name not in payload:
            continue
        try:
            number = float(payload[name])
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            state.set_axis(name, clamp(number, *AXIS_RANGES[name]))
    extra = payload.get("extra")
    if isinstance(extra, dict):
        state.extra = {
            str(k): float(v)
            for k, v in extra.items()
            if isinstance(v, (int, float)) and math.isfinite(float(v))
        }
    for name in ("last_activity_at", "last_tick", "last_spoke_at"):
        raw = payload.get(name)
        if isinstance(raw, str) and raw:
            try:
                setattr(state, name, datetime.fromisoformat(raw))
            except ValueError:
                pass
    gates = payload.get("last_gates")
    if isinstance(gates, list):
        state.last_gates = [str(gate) for gate in gates]
    if payload.get("user_state"):
        state.user_state = str(payload["user_state"])
    return state


def load_state(persona_id: str | None = None, *, path: Path | None = None) -> tuple[State, list[str]]:
    target = path or state_path(persona_id)
    if not target.is_file():
        return State(), []
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return State(), [f"{target.name}: 读不了（{exc}），从零开始"]
    return state_from_dict(payload), []


def save_state(persona_id: str | None, state: State, *, path: Path | None = None) -> Path:
    """写状态。与星图同一纪律：只新增自己的文件，绝不碰记忆正文。"""
    target = path or state_path(persona_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state_to_dict(state), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def note_spoke(persona_id: str | None = None, *, now: datetime | None = None, path: Path | None = None) -> State:
    """记下"她刚开口了"——冷却靠它。"""
    stamp = now or _now()
    state, _ = load_state(persona_id, path=path)
    state.last_spoke_at = stamp
    save_state(persona_id, state, path=path)
    return state


def on_user_message(persona_id: str | None = None, *, path: Path | None = None) -> dict:
    """对方回话了：等待由"你出现了"解除。

    由网页聊天链在存下 user 消息之后调用（`app/main.py`）。
    不碰 `last_tick`——时间照常流逝，只是积温回落一截。
    """
    config, _ = load_config(persona_id)
    state, _ = load_state(persona_id, path=path)
    engine = Engine(config, state)
    engine.apply_delta(**config.reply_delta)
    save_state(persona_id, engine.state, path=path)
    return engine.snapshot()


def tick_minutes(persona_id: str | None = None) -> float:
    """空闲 tick 的间隔：读 `schedule.txt` 里那个一直没人用的 `tick_minutes`。"""
    from . import supervisor

    try:
        raw = supervisor.parse_schedule(persona_id).get("tick_minutes")
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else 20.0


def seconds_until_due(persona_id: str | None = None, *, now: datetime | None = None) -> float:
    """距下一次空闲 tick 还有多少秒；`0` = 现在就该跑（含第一次）。"""
    now = now or _now()
    state, _ = load_state(persona_id)
    if state.last_tick is None:
        return 0.0
    elapsed = (now - state.last_tick).total_seconds()
    return max(0.0, tick_minutes(persona_id) * 60.0 - elapsed)


def ambient_tick(
    persona_id: str | None = None,
    *,
    now: datetime | None = None,
    minutes_since_message: float | None = None,
    config: Config | None = None,
    path: Path | None = None,
    log: Path | None = None,
) -> dict:
    """空闲 tick：推进积温、落盘、返回这次触发了什么。

    第一次调用只记时间（不推进）——否则重启一次就会凭空老去一大截。
    """
    stamp = now or _now()
    resolved = config or load_config(persona_id)[0]
    state, notes = load_state(persona_id, path=path)
    if state.last_tick is None:
        state.last_tick = stamp
        save_state(persona_id, state, path=path)
        return {"elapsed": 0.0, "first": True, "fired": [], "notes": notes, "snapshot": Engine(resolved, state).snapshot()}

    elapsed = (stamp - state.last_tick).total_seconds() / 60.0
    if elapsed <= 0:
        return {"elapsed": 0.0, "first": False, "fired": [], "notes": notes, "snapshot": Engine(resolved, state).snapshot()}

    engine = Engine(resolved, state)
    fired = engine.tick(elapsed, now=stamp, minutes_since_message=minutes_since_message)
    gates_now = gate_set(engine, fired)
    loggable = gates_now != sorted(engine.state.last_gates or [])
    engine.state.last_gates = gates_now
    applied = min(elapsed, engine.config.max_tick_minutes)
    save_state(persona_id, engine.state, path=path)
    result = {
        "elapsed": round(elapsed, 2),
        "applied": round(applied, 2),
        "capped": elapsed > engine.config.max_tick_minutes,
        "first": False,
        "fired": fired,
        "gates": gates_now,
        "explain": engine.explain(),
        "style": engine.style_guidance(),
        "hook": engine.prompt_context(),
        "snapshot": engine.snapshot(),
        "notes": notes,
    }
    if loggable:
        result["logged"] = append_log(
            persona_id,
            {
                "kind": "tick",
                "at": stamp.isoformat(timespec="seconds"),
                "minutes": result["elapsed"],
                "applied": result["applied"],
                "gates": [entry["gate"] for entry in fired],
                "explain": result["explain"],
                "style": result["style"],
                **result["snapshot"],
            },
            path=log,
        )
    return result


# ── 台账与标注 ──────────────────────────────────────
LOG_NAME = "warmth-log.jsonl"
APPROACH_RATIO = 0.8  # connection 到了有效门槛的八成，就算"快憋不住了"，值得记一笔


def log_path(persona_id: str | None = None) -> Path:
    from . import persona

    return persona.folder(persona_id) / "memory" / LOG_NAME


def gate_set(engine: "Engine", fired: list[dict]) -> list[str]:
    """这一拍的"闸门画像"：触发了什么 + 是否已经逼近开口线。"""
    gates = {entry["gate"] for entry in fired}
    if engine.state.connection >= engine.effective_thresholds()["contact"] * APPROACH_RATIO:
        gates.add("approaching")
    return sorted(gates)


def interesting(engine: "Engine", fired: list[dict], *, previous: list[str] | None = None) -> bool:
    """只记**画像变了**的那一拍。

    两次修正的教训：
    1. 早先只要"有触发"就记，而 `observation` 从 connection≥0.20 起每一拍都成立 → 24 小时 72/72 行；
    2. 改成"开口类每次也记"之后，`contact` 一旦触发同样每拍都成立 → 24 小时仍有 69 行。
    所以只认变化：跨线、被挡住、冷却开始/结束各记一次。真正说出口另由心跳记 `kind=speak`。
    """
    current = gate_set(engine, fired)
    before = sorted(engine.state.last_gates or []) if previous is None else sorted(previous)
    return current != before


def append_log(persona_id: str | None, entry: dict, *, path: Path | None = None) -> Path:
    target = path or log_path(persona_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return target


def read_log(
    persona_id: str | None = None,
    *,
    days: float | None = None,
    path: Path | None = None,
    now: datetime | None = None,
) -> list[dict]:
    target = path or log_path(persona_id)
    if not target.is_file():
        return []
    cutoff = None
    if days:
        cutoff = (now or _now()) - timedelta(days=days)
    rows: list[dict] = []
    for raw in target.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if cutoff is not None:
            stamp = _parse_stamp(row.get("at"))
            if stamp is None or stamp < cutoff:
                continue
        rows.append(row)
    return rows


def _parse_stamp(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


LABELS = ("should_speak", "ok_silent", "na")


def mark(
    persona_id: str | None = None,
    text: str = "",
    *,
    label: str = "should_speak",
    path: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """给此刻贴一条标注：她该开口 / 沉默是对的 / 说不清。

    这是阈值校准的唯一依据——先攒 10–20 条，再动那几个数。
    """
    stamp = now or _now()
    state, _ = load_state(persona_id)
    entry = {
        "kind": "mark",
        "at": stamp.isoformat(timespec="seconds"),
        "label": label if label in LABELS else "na",
        "text": str(text or "").strip(),
        **state_to_dict(state),
    }
    append_log(persona_id, entry, path=path)
    return entry


def review(persona_id: str | None = None, *, days: float = 3, limit: int = 60, path: Path | None = None, now: datetime | None = None) -> str:
    """把最近的台账排成能一眼扫过的样子——校准阈值前先看这个。"""
    rows = read_log(persona_id, days=days, path=path, now=now)
    if not rows:
        return f"近 {days:g} 天没有记录（她还没醒过，或者一直没逼近门槛）"
    ticks = [row for row in rows if row.get("kind") == "tick"]
    marks = [row for row in rows if row.get("kind") == "mark"]
    lines = [f"近 {days:g} 天：{len(ticks)} 次值得记的 tick，{len(marks)} 条人工标注"]
    if marks:
        lines.append("人工标注：")
        for row in marks[-limit:]:
            lines.append(f"  {_short_time(row.get('at'))}  [{row.get('label')}] {row.get('text') or '（无说明）'}")
    lines.append("最近的动作：")
    for row in ticks[-limit:]:
        gates = "/".join(row.get("gates") or []) or "无触发"
        lines.append(
            f"  {_short_time(row.get('at'))}  c{_num(row.get('connection'))} p{_num(row.get('pride'))}"
            f" v{_num(row.get('valence'))} a{_num(row.get('arousal'))} i{_num(row.get('immersion'))}"
            f" h{_num(row.get('hesitation'))}  {gates}"
        )
    return "\n".join(lines)


def _short_time(raw) -> str:
    stamp = _parse_stamp(raw)
    return stamp.strftime("%m-%d %H:%M") if stamp else "??"


def _num(value) -> str:
    try:
        return f"{float(value):+.2f}"
    except (TypeError, ValueError):
        return " ?  "


@dataclass
class State:
    connection: float = 0.0
    pride: float = 0.0
    valence: float = 0.0
    arousal: float = 0.0
    immersion: float = 0.0
    hesitation: float = 0.0
    extra: dict[str, float] = field(default_factory=dict)
    last_activity_at: datetime | None = None
    last_tick: datetime | None = None
    last_spoke_at: datetime | None = None
    last_gates: list[str] = field(default_factory=list)
    user_state: str = "active"

    def axis(self, name: str) -> float:
        if name in self.extra:
            return self.extra[name]
        return float(getattr(self, name, 0.0))

    def set_axis(self, name: str, value: float) -> None:
        if name in self.extra:
            self.extra[name] = value
        else:
            setattr(self, name, value)


class Engine:
    """确定性积温引擎。同一状态 + 同一输入 → 同一输出，没有随机数。"""

    def __init__(self, config: Config | None = None, state: State | None = None) -> None:
        self.config = config or defaults()
        self.state = state or State()
        for spec in self.config.extra_axes:
            self.state.extra.setdefault(spec.name, spec.initial)
        self.last_trace: list[dict] = []

    # ── 外部可以推一把 ────────────────────────────────
    def set_activity(self, kind: str | None, *, at: datetime | None = None) -> None:
        """她在做事 → immersion 抬起来；`None` 表示停下来。

        与参照一致（jiwen.js:554-573）：未知类型按 0.2，设定时下限 0.01——有活动就不会是 0。
        """
        mapping = {"reading": 0.6, "search": 0.4, "browse_snitch": 0.35, "browse": 0.35, "observe": 0.15}
        if kind:
            self.state.immersion = max(0.01, mapping.get(kind, 0.2))
            self.state.last_activity_at = at or _now()
        else:
            self.state.immersion = 0.0
            self.state.last_activity_at = None

    def set_user_state(self, value: str) -> None:
        """由轻量模型从最近对话推断：active / busy / away / sleeping。"""
        if value in self.config.user_state_lift:
            self.state.user_state = value

    def apply_delta(self, **deltas: float) -> None:
        """外部（通常是 LLM 分析）注入一次性增减。

        比参照实现多一道闸：**非有限值直接丢弃**。那边没有 NaN 防护，
        `applyDelta({valence: NaN})` 会永久污染该轴（clamp 会让 NaN 穿透）。
        """
        for name, value in deltas.items():
            if name not in AXIS_RANGES and name not in self.state.extra:
                continue
            number = float(value)
            if not math.isfinite(number):
                continue
            low, high = self._range_of(name)
            self.state.set_axis(name, clamp(self.state.axis(name) + number, low, high))

    def _range_of(self, name: str) -> tuple[float, float]:
        if name in AXIS_RANGES:
            return AXIS_RANGES[name]
        for spec in self.config.extra_axes:
            if spec.name == name:
                return spec.low, spec.high
        return (-1.0, 1.0)

    # ── 推进 ────────────────────────────────────────
    def tick(self, minutes: float, *, now: datetime | None = None, minutes_since_message: float | None = None) -> list[dict]:
        """推进 `minutes` 分钟并返回本次触发的动作。`minutes <= 0` 原样返回空。"""
        if not minutes or minutes <= 0:
            return []
        mins = min(float(minutes), self.config.max_tick_minutes)
        stamp = now or _now()
        rates = self.config.rates
        state = self.state

        self._drift_connection(mins, rates, minutes_since_message)
        self._decay_immersion(mins, rates, stamp)
        self._drift_pride(mins, rates)
        self._drift_valence(mins, rates)
        self._drift_arousal(mins, rates)
        self._drift_hesitation(mins, rates)
        self._drift_extra(mins)

        state.last_tick = stamp
        self.last_trace = self.check()
        return [entry for entry in self.last_trace if entry["fired"]]

    # ── 漂移 ────────────────────────────────────────
    def _drift_connection(self, mins: float, rates: Rates, minutes_since_message: float | None) -> None:
        accel = 1.0
        if rates.connection_accel > 0 and minutes_since_message is not None and minutes_since_message >= rates.accel_delay:
            accel = math.pow(1.0 + self.state.connection, rates.connection_accel)
        valence_multiplier = 1.0
        if rates.valence_connect_dampen > 0 and self.state.valence < rates.valence_connect_dampen_threshold:
            valence_multiplier = rates.valence_connect_dampen  # 严重低落 → 自我封闭，涨得更慢
        elif rates.valence_connect_boost > 0 and self.state.valence < rates.valence_connect_boost_threshold:
            valence_multiplier = rates.valence_connect_boost  # 轻度不开心 → 想要安慰，涨得更快
        dampen = 1.0
        if rates.immersion_dampen_connection > 0:
            dampen = max(0.0, 1.0 - self.state.immersion * rates.immersion_dampen_connection)
        rate = rates.connection_base * accel * valence_multiplier * dampen
        low, high = AXIS_RANGES["connection"]
        self.state.connection = clamp(self.state.connection + rate * mins, low, high)

    def _decay_immersion(self, mins: float, rates: Rates, now: datetime) -> None:
        state = self.state
        if state.last_activity_at is None:
            return
        since = max(0.0, (now - state.last_activity_at).total_seconds() / 60.0)
        state.immersion = max(0.0, state.immersion - rates.immersion_decay * min(mins, since))
        if state.immersion <= 0.01 and since > 60:
            state.last_activity_at = None
            state.immersion = 0.0

    def _drift_pride(self, mins: float, rates: Rates) -> None:
        state = self.state
        low, high = AXIS_RANGES["pride"]
        if state.connection >= rates.pride_defend_threshold:
            target = clamp(rates.pride_defend_target, low, high)
            state.pride = _approach(state.pride, target, rates.pride_defend_rate * mins)
        else:
            state.pride = _approach(state.pride, 0.0, rates.pride_regress * mins)

    def _drift_valence(self, mins: float, rates: Rates) -> None:
        state = self.state
        low, high = AXIS_RANGES["valence"]
        regress = rates.valence_regress
        if state.connection >= rates.valence_lock_threshold:
            regress *= rates.valence_lock_factor
        state.valence = _approach(state.valence, clamp(rates.valence_setpoint, low, high), regress * mins)
        if rates.valence_connection_drift_rate > 0 and state.connection >= rates.valence_connection_drift_threshold:
            drift = rates.valence_connection_drift_rate * mins * state.connection
            state.valence = clamp(state.valence - drift, low, high)

    def _drift_arousal(self, mins: float, rates: Rates) -> None:
        state = self.state
        low, high = AXIS_RANGES["arousal"]
        setpoint = clamp(rates.arousal_setpoint, low, high)
        regress_rate = rates.arousal_regress
        if state.valence >= rates.arousal_lock_threshold:
            regress_rate *= rates.arousal_lock_factor
        force = 0.0
        if state.arousal > setpoint:
            force = -regress_rate * mins
        elif state.arousal < setpoint:
            force = regress_rate * mins
        rise = 0.0
        if state.connection >= rates.arousal_connection_rise_threshold:
            rise += rates.arousal_connection_rise_rate * mins
        if (
            rates.pride_arousal_conflict_rate > 0
            and state.connection >= self.config.thresholds.consider_contact
            and state.pride >= self.config.thresholds.pride_block
        ):
            rise += rates.pride_arousal_conflict_rate * mins
        net = state.arousal + force + rise
        if rise == 0 and ((force < 0 and net < setpoint) or (force > 0 and net > setpoint)):
            state.arousal = setpoint  # 回归力不会推过设定点
        else:
            state.arousal = clamp(net, low, high)

    def _drift_hesitation(self, mins: float, rates: Rates) -> None:
        """犹豫：只要她还想找他、又还端着，就一直在咽。

        早先只在 `[floor, ceiling)` 这个带里长，而那个带只有两个多小时宽——
        骄傲爬到防御目标时它已经结束了，犹豫永远到不了"欲言又止"的强度。
        现在没有上界：**越久越咽回去**，这才符合它要表达的东西。
        """
        state = self.state
        thresholds = self.config.thresholds
        low, high = AXIS_RANGES["hesitation"]
        conflicted = state.connection >= rates.hesitation_source_floor and state.pride >= thresholds.pride_block
        if conflicted:
            state.hesitation = clamp(state.hesitation + rates.hesitation_rise * mins, low, high)
        else:
            state.hesitation = clamp(state.hesitation - rates.hesitation_decay * mins, low, high)
        if state.user_state == "busy":
            state.hesitation = clamp(state.hesitation + rates.hesitation_rise * 0.5 * mins, low, high)

    def _drift_extra(self, mins: float) -> None:
        for spec in self.config.extra_axes:
            value = self.state.extra.get(spec.name, spec.initial)
            if spec.kind == "driven":
                if self.state.axis(spec.source) >= spec.threshold:
                    value += spec.rate * mins
            else:
                value = _approach(value, spec.setpoint, spec.rate * mins)
            self.state.extra[spec.name] = clamp(value, spec.low, spec.high)

    # ── 闸门 ────────────────────────────────────────
    def effective_thresholds(self) -> dict[str, float]:
        """`user_state` 与犹豫把门槛往上抬——把"角色中心"推向"关系中心"。"""
        thresholds = replace(self.config.thresholds)
        lift = self.config.user_state_lift.get(self.state.user_state, 1.0)
        contact = thresholds.consider_contact * lift + thresholds.hesitation_lift * self.state.hesitation
        return {
            "observation": thresholds.observation * lift,
            "contact": contact,
            "force": thresholds.force_contact * lift,
            "pride_block": thresholds.pride_block,
            "valence_activity": thresholds.valence_activity,
            "arousal_agitation": thresholds.arousal_agitation,
        }

    def cooldown_left(self) -> float:
        """距上次开口还差多少分钟才允许再开口；`0` = 可以。"""
        if self.state.last_spoke_at is None or self.config.speak_cooldown_minutes <= 0:
            return 0.0
        now = self.state.last_tick or self.state.last_spoke_at
        elapsed = (now - self.state.last_spoke_at).total_seconds() / 60.0
        return max(0.0, self.config.speak_cooldown_minutes - elapsed)

    def check(self) -> list[dict]:
        """逐闸门报"过了没有、被谁挡的、差多少"。

        开口类**三选一**（observation / contact / find_activity / 强制 contact 互斥），
        自我调节类最多再追加一个——所以触发数只可能是 0、1、2，与参照实现一致。
        """
        state = self.state
        gate = self.effective_thresholds()
        trace: list[dict] = []
        opening: dict | None = None
        held = False  # 已经在别处写下了"为什么没说"，就不要补一条会误导的 opening

        if state.connection >= gate["force"]:
            opening = _entry(
                "force_contact",
                True,
                f"connection {state.connection:.3f} ≥ 强制线 {gate['force']:.3f}，骄傲挡不住",
                urgency=min(1.0, state.connection - gate["force"]),
            )
        elif state.connection >= gate["contact"]:
            if state.pride < gate["pride_block"]:
                opening = _entry(
                    "contact",
                    True,
                    f"pride {state.pride:.3f} < {gate['pride_block']:.3f}",
                    urgency=state.connection - gate["contact"],
                )
            elif state.immersion < BUFFER_IMMERSION:
                opening = _entry(
                    "find_activity",
                    True,
                    f"pride {state.pride:.3f} 挡住了开口，但 immersion {state.immersion:.2f} < {BUFFER_IMMERSION}",
                )
            else:
                trace.append(
                    _entry(
                        "blocked",
                        False,
                        f"pride {state.pride:.3f} ≥ {gate['pride_block']:.3f} 且 immersion {state.immersion:.2f}，被缓冲住",
                        gap=state.pride - gate["pride_block"],
                    )
                )
                held = True  # 已经写下"被挡住"，不要再补一条 gap 为负的 opening
        elif state.connection >= gate["observation"]:
            span = max(1e-6, gate["contact"] - gate["observation"])
            opening = _entry(
                "observation",
                True,
                f"connection {state.connection:.3f} 在观察带里",
                urgency=(state.connection - gate["observation"]) / span,
            )

        cooldown = self.cooldown_left()
        if opening is not None and opening["gate"] in {"contact", "force_contact"} and cooldown > 0:
            # 冷却：刚开口没多久就不再重复。否则她一跨线就会每 20 分钟说一次，
            # 把当天的配额在一小时里烧完。
            trace.append(
                _entry(
                    "cooldown",
                    False,
                    f"想开口，但距上次开口只过了 "
                    f"{self.config.speak_cooldown_minutes - cooldown:.0f} 分钟"
                    f"（冷却 {self.config.speak_cooldown_minutes:g} 分钟）",
                    gap=cooldown,
                )
            )
            opening = None
            held = True

        if opening is not None:
            trace.append(opening)
        elif not held:
            trace.append(
                _entry(
                    "opening",
                    False,
                    f"connection {state.connection:.3f} < 观察线 {gate['observation']:.3f}"
                    + ("（含犹豫与 user_state 抬升）" if gate["contact"] > self.config.thresholds.consider_contact else ""),
                    gap=gate["observation"] - state.connection,
                )
            )

        if state.immersion < REGULATE_IMMERSION and (
            state.valence <= gate["valence_activity"] or state.arousal >= gate["arousal_agitation"]
        ):
            reason = "low_valence" if state.valence <= gate["valence_activity"] else "high_arousal"
            trace.append(
                _entry(
                    "find_activity",
                    True,
                    f"{reason}：valence {state.valence:.3f} / arousal {state.arousal:.3f}",
                    urgency=min(1.0, max(abs(state.valence), state.arousal)),
                )
            )
        return trace

    def explain(self, trace: list[dict] | None = None) -> str:
        """一句话：这次为什么没说。"""
        entries = trace if trace is not None else self.last_trace
        fired = [entry for entry in entries if entry["fired"]]
        if fired:
            return "触发：" + "、".join(f"{entry['gate']}（{entry['detail']}）" for entry in fired)
        nearest = min((entry for entry in entries if entry.get("gap") is not None), key=lambda e: e["gap"], default=None)
        if nearest is None:
            return "没触发，也没有可比的差距"
        return f"没触发：最接近的是 {nearest['gate']}，还差 {nearest['gap']:.3f}（{nearest['detail']}）"

    # ── 翻成人话 ────────────────────────────────────
    def prompt_context(self) -> str:
        """给说话模型看的几句话——它不需要知道 `connection=0.47`。"""
        state = self.state
        parts: list[str] = []
        if state.connection >= self.config.thresholds.force_contact:
            parts.append("你很想找对方，已经压不住了")
        elif state.connection >= self.config.thresholds.consider_contact:
            if state.pride >= self.config.thresholds.pride_block:
                parts.append("想找对方，但拉不下脸")
            else:
                parts.append("有点想找对方")
        elif state.connection >= self.config.thresholds.observation:
            parts.append("偶尔会想起对方")
        if state.valence <= -0.4:
            parts.append("心情低")
        elif state.valence >= 0.4:
            parts.append("心情不错")
        if state.arousal >= 0.7:
            parts.append("坐不太住")
        if state.immersion >= 0.4 and state.last_activity_at is not None:
            parts.append("手头正有事做，注意力被占着")
        if state.hesitation >= 0.5:
            parts.append("话到嘴边又咽回去了")
        return "；".join(parts) if parts else "平静"

    def style_guidance(self) -> str:
        """给这一句的写法提示。"""
        state = self.state
        if state.hesitation >= 0.5 and state.connection >= self.config.thresholds.consider_contact:
            return "欲言又止：短、绕、先起个头就停，别一口气说完"
        if state.valence <= -0.4 and state.arousal >= 0.5:
            return "烦躁带刺：句子短，容易呛人"
        if state.valence <= -0.4:
            return "低落话少：不解释，不回问，一两个词也够"
        if state.arousal >= 0.7:
            return "急着说：连着发，不等对方回"
        return "照常"

    def snapshot(self) -> dict:
        return {
            "connection": round(self.state.connection, 4),
            "pride": round(self.state.pride, 4),
            "valence": round(self.state.valence, 4),
            "arousal": round(self.state.arousal, 4),
            "immersion": round(self.state.immersion, 4),
            "hesitation": round(self.state.hesitation, 4),
            "extra": {k: round(v, 4) for k, v in self.state.extra.items()},
            "user_state": self.state.user_state,
            "thresholds": {k: round(v, 4) for k, v in self.effective_thresholds().items()},
        }


def _entry(gate: str, fired: bool, detail: str, *, gap: float | None = None, urgency: float | None = None) -> dict:
    """一条闸门记录。`gap` = 还差多少，`urgency` = 过了多少（0–1，可当强度用）。"""
    return {
        "gate": gate,
        "fired": bool(fired),
        "detail": detail,
        "gap": gap,
        "urgency": None if urgency is None else round(max(0.0, min(1.0, urgency)), 4),
    }


def _approach(value: float, target: float, step: float) -> float:
    """朝目标走一步，但绝不越过目标。"""
    if value > target:
        return max(target, value - step)
    if value < target:
        return min(target, value + step)
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def run(engine: Engine, steps: Iterable[tuple[float, float | None]]) -> list[list[dict]]:
    """跑一串 (分钟, 距上次消息分钟) —— 调参与测试用的脚本式驱动。"""
    out: list[list[dict]] = []
    for minutes, since_message in steps:
        out.append(engine.tick(minutes, minutes_since_message=since_message))
    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="积温引擎：干跑一条时间线，看闸门怎么动")
    parser.add_argument("--persona", default=None)
    parser.add_argument("--minutes", type=float, default=20.0, help="每步推进多少分钟")
    parser.add_argument("--steps", type=int, default=12)
    parser.add_argument("--user-state", default="active")
    parser.add_argument("--since-message", type=float, default=None, help="距上次消息多少分钟（--once 用）")
    parser.add_argument("--write-template", action="store_true", help="把特化模板写到 persona/<id>/memory/warmth.json")
    parser.add_argument("--overwrite", action="store_true", help="配合 --write-template：覆盖已有文件")
    parser.add_argument("--show", action="store_true", help="只显示当前生效的旋钮与配置问题")
    parser.add_argument("--once", action="store_true", help="跑一次真实的空闲 tick（会落盘状态）")
    parser.add_argument("--state", action="store_true", help="显示已保存的状态与距下次到点还有多久")
    parser.add_argument("--mark", default=None, help="给此刻贴一条人工标注（配合 --label）")
    parser.add_argument("--label", default="should_speak", choices=list(LABELS), help="标注结论")
    parser.add_argument("--review", action="store_true", help="列出最近的台账与人工标注")
    parser.add_argument("--days", type=float, default=3.0, help="配合 --review：看最近几天")
    options = parser.parse_args()

    from . import persona

    pid = options.persona or persona.active_id()

    if options.mark is not None:
        print(json.dumps(mark(pid, options.mark, label=options.label), ensure_ascii=False, indent=2))
        raise SystemExit(0)

    if options.review:
        print(review(pid, days=options.days))
        raise SystemExit(0)

    if options.write_template:
        target = write_template(pid, overwrite=options.overwrite)
        print(f"模板：{target}")
        raise SystemExit(0)

    config, notes = load_config(pid)
    if options.show:
        print(json.dumps(effective_config(config), ensure_ascii=False, indent=2))
        for note in notes:
            print(f"· {note}")
        raise SystemExit(0)

    if options.state:
        state, state_notes = load_state(pid)
        print(f"状态：{state_path(pid)}")
        print(json.dumps(state_to_dict(state), ensure_ascii=False, indent=2))
        print(f"空闲 tick 间隔：{tick_minutes(pid)} 分钟；距下一次 {seconds_until_due(pid):.0f} 秒")
        for note in state_notes:
            print(f"· {note}")
        raise SystemExit(0)

    if options.once:
        result = ambient_tick(pid, minutes_since_message=options.since_message)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        for note in result.get("notes", []):
            print(f"· {note}")
        raise SystemExit(0)

    engine = Engine(config)
    engine.set_user_state(options.user_state)
    print(f"persona {pid}  配置 {config_path(pid)}" + ("（未找到，用默认）" if not config_path(pid).is_file() else ""))
    for note in notes:
        print(f"· {note}")
    for index in range(options.steps):
        elapsed = (index + 1) * options.minutes
        triggers = engine.tick(options.minutes, minutes_since_message=elapsed)
        print(f"{elapsed:6.0f} 分钟  {json.dumps(engine.snapshot(), ensure_ascii=False)}")
        if triggers:
            print(f"        → {engine.explain()}  |  {engine.style_guidance()}")
