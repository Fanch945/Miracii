"""积温引擎：确定性、漂移、闸门、犹豫与 user_state 的调制。

全部在内存里跑，不碰记忆，也不写盘。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import persona, warmth


def engine(**kwargs) -> warmth.Engine:
    """带覆盖的引擎：`thresholds__x=1` / `rates__y=2` 直接改到对应 dataclass。"""
    config = warmth.Config()
    for key, value in kwargs.items():
        group, _, fieldname = key.partition("__")
        target = getattr(config, group)
        config = replace(config, **{group: replace(target, **{fieldname: value})})
    return warmth.Engine(config)


class DeterminismTests(unittest.TestCase):
    def test_same_input_same_output(self):
        first, second = warmth.Engine(), warmth.Engine()
        steps = [(15.0, 20.0), (15.0, 35.0), (15.0, 50.0), (15.0, 65.0)]
        self.assertEqual(warmth.run(first, steps), warmth.run(second, steps))
        self.assertEqual(first.snapshot(), second.snapshot())

    def test_no_randomness_anywhere_in_the_module(self):
        source = (warmth.__file__ or "")
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("import random", text)
        self.assertNotIn("Math.random", text)


class TickGuardTests(unittest.TestCase):
    def test_zero_or_negative_minutes_is_a_no_op(self):
        eng = engine()
        before = eng.snapshot()
        self.assertEqual(eng.tick(0), [])
        self.assertEqual(eng.tick(-5), [])
        self.assertEqual(eng.snapshot(), before)

    def test_a_long_gap_is_capped_at_sixty_minutes(self):
        capped, straight = engine(), engine()
        capped.tick(600)
        straight.tick(60)
        self.assertAlmostEqual(capped.state.connection, straight.state.connection, places=9)


class DriftTests(unittest.TestCase):
    def test_connection_only_grows_and_stays_in_range(self):
        eng = engine()
        for _ in range(200):
            eng.tick(60)
        self.assertGreater(eng.state.connection, 0.0)
        self.assertLessEqual(eng.state.connection, 1.0)

    def test_acceleration_kicks_in_after_the_delay(self):
        early, late = engine(), engine()
        early.tick(10, minutes_since_message=10)  # 未到 accel_delay(45)
        late.tick(10, minutes_since_message=120)  # 已过
        self.assertAlmostEqual(early.state.connection, late.state.connection, places=9)
        early.state.connection = late.state.connection = 0.5
        early.tick(10, minutes_since_message=10)
        late.tick(10, minutes_since_message=120)
        self.assertGreater(late.state.connection, early.state.connection)

    def test_immersion_decays_and_then_clears(self):
        eng = engine()
        start = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        eng.set_activity("reading", at=start)
        self.assertAlmostEqual(eng.state.immersion, 0.6)
        eng.tick(10, now=start + timedelta(minutes=10))
        self.assertLess(eng.state.immersion, 0.6)
        eng.state.immersion = 0.005
        eng.tick(10, now=start + timedelta(minutes=90))
        self.assertEqual(eng.state.immersion, 0.0)
        self.assertIsNone(eng.state.last_activity_at)

    def test_unknown_activity_still_occupies_attention(self):
        eng = engine()
        eng.set_activity("nonsense")
        self.assertAlmostEqual(eng.state.immersion, 0.2)
        eng.set_activity(None)
        self.assertEqual(eng.state.immersion, 0.0)
        self.assertIsNone(eng.state.last_activity_at)

    def test_nan_never_poisons_an_axis(self):
        """参照实现没有这道闸：NaN 会穿过 clamp 永久污染该轴。"""
        eng = engine()
        eng.apply_delta(valence=float("nan"), arousal=float("inf"), pride=0.2)
        self.assertEqual(eng.state.valence, 0.0)
        self.assertAlmostEqual(eng.state.pride, 0.2)

    def test_pride_regresses_to_zero_unless_defence_is_armed(self):
        calm = engine()
        calm.state.pride = 0.8
        calm.tick(60)
        self.assertLess(calm.state.pride, 0.8)

        defensive = engine(rates__pride_defend_threshold=0.1, rates__pride_defend_rate=0.02)
        defensive.state.connection = 0.5
        defensive.tick(10)
        self.assertGreater(defensive.state.pride, 0.0)

    def test_valence_sinks_while_waiting_and_regresses_when_calm(self):
        waiting = engine()
        waiting.state.connection = 0.6
        waiting.tick(60)
        self.assertLess(waiting.state.valence, 0.0)

        calm = engine()
        calm.state.valence = -0.5
        calm.tick(60)
        self.assertGreater(calm.state.valence, -0.5)

    def test_valence_lock_slows_the_return(self):
        locked = engine(rates__valence_lock_threshold=0.1, rates__valence_lock_factor=0.1)
        free = engine()
        for eng in (locked, free):
            eng.state.valence = -0.8
            eng.state.connection = 0.5
            eng.state.user_state = "active"
        locked.tick(30)
        free.tick(30)
        self.assertLess(locked.state.valence, free.state.valence)  # 锁住的那条回得更慢，仍更接近 -0.8

    def test_arousal_regresses_toward_the_setpoint_without_overshooting(self):
        eng = engine()
        eng.state.arousal = 0.4
        eng.tick(10)  # connection 仍低，只有回归力：0.005 × 10
        self.assertAlmostEqual(eng.state.arousal, 0.35, places=6)

    def test_arousal_snaps_to_the_setpoint_when_the_step_would_pass_it(self):
        eng = engine()
        eng.state.arousal = 0.02
        eng.tick(10)
        self.assertEqual(eng.state.arousal, 0.0)

    def test_arousal_rises_while_waiting(self):
        eng = engine()
        eng.state.connection = 0.4
        eng.tick(30)
        self.assertGreater(eng.state.arousal, 0.0)

    def test_values_never_leave_their_ranges(self):
        eng = engine(rates__connection_base=5.0)
        eng.tick(60)
        self.assertLessEqual(eng.state.connection, 1.0)
        eng.apply_delta(valence=-99, arousal=99, nonsense=1)
        self.assertGreaterEqual(eng.state.valence, -1.0)
        self.assertLessEqual(eng.state.arousal, 1.0)


class GateTests(unittest.TestCase):
    def test_silence_below_the_first_gate_and_the_gap_is_reported(self):
        eng = engine()
        self.assertEqual(eng.tick(1), [])
        self.assertIn("没触发", eng.explain())
        self.assertIn("还差", eng.explain())

    def test_contact_needs_connection_but_not_pride(self):
        eng = engine()
        eng.state.connection = 0.40
        fired = eng.tick(1)
        self.assertEqual([entry["gate"] for entry in fired], ["contact"])
        self.assertIsNotNone(fired[0]["urgency"])

    def test_opening_gates_are_mutually_exclusive(self):
        """observation 与 contact 是同一组的三选一分支，不会同时触发。"""
        eng = engine()
        eng.state.connection = 0.25
        self.assertEqual([entry["gate"] for entry in eng.tick(1)], ["observation"])

        eng.state.connection = 0.60
        self.assertEqual([entry["gate"] for entry in eng.tick(1)], ["force_contact"])

    def test_pride_blocks_contact_and_turns_it_into_an_activity(self):
        eng = engine()
        eng.state.connection = 0.40
        eng.state.pride = 0.8
        eng.state.immersion = 0.1
        gates = [entry["gate"] for entry in eng.tick(1)]
        self.assertIn("find_activity", gates)
        self.assertNotIn("contact", gates)

    def test_pride_with_immersion_is_simply_buffered(self):
        eng = engine()
        eng.state.connection = 0.40
        eng.state.pride = 0.8
        eng.state.immersion = 0.5
        gates = [entry["gate"] for entry in eng.tick(1)]
        self.assertNotIn("contact", gates)
        self.assertNotIn("find_activity", gates)
        blocked = [entry for entry in eng.last_trace if entry["gate"] == "blocked"]
        self.assertTrue(blocked and "被缓冲住" in blocked[0]["detail"])

    def test_forced_contact_ignores_pride(self):
        eng = engine()
        eng.state.connection = 0.60
        eng.state.pride = 0.9
        gates = [entry["gate"] for entry in eng.tick(1)]
        self.assertIn("force_contact", gates)

    def test_low_valence_with_low_immersion_triggers_self_regulation(self):
        eng = engine(thresholds__valence_activity=-0.5)  # 默认 -1.0 = 永不，与参照实现一致
        eng.state.valence = -0.9
        eng.state.immersion = 0.1
        gates = [entry["gate"] for entry in eng.tick(1) if entry["fired"]]
        self.assertIn("find_activity", gates)


class ModulationTests(unittest.TestCase):
    def test_hesitation_raises_the_contact_threshold(self):
        def fired(eng) -> list[str]:
            return [entry["gate"] for entry in eng.check() if entry["fired"]]

        plain = engine()
        plain.state.connection = 0.40
        self.assertIn("contact", fired(plain))

        hesitant = engine()
        hesitant.state.connection = 0.40
        hesitant.state.hesitation = 1.0
        self.assertNotIn("contact", fired(hesitant))

    def test_hesitation_grows_in_the_consider_band_when_proud(self):
        eng = engine()
        eng.state.connection = 0.40
        eng.state.pride = 0.8
        eng.tick(30)
        self.assertGreater(eng.state.hesitation, 0.0)

    def test_busy_user_pushes_the_thresholds_up(self):
        def fired(eng) -> list[str]:
            return [entry["gate"] for entry in eng.check() if entry["fired"]]

        free, busy = engine(), engine()
        for eng in (free, busy):
            eng.state.connection = 0.40
        busy.set_user_state("busy")
        self.assertIn("contact", fired(free))
        self.assertNotIn("contact", fired(busy))
        self.assertGreater(busy.effective_thresholds()["contact"], free.effective_thresholds()["contact"])

    def test_sleeping_is_even_more_restrained(self):
        eng = engine()
        eng.set_user_state("sleeping")
        self.assertGreater(eng.effective_thresholds()["contact"], engine().effective_thresholds()["contact"] * 1.5)

    def test_unknown_user_state_is_ignored(self):
        eng = engine()
        eng.set_user_state("nonsense")
        self.assertEqual(eng.state.user_state, "active")


class VoiceTests(unittest.TestCase):
    def test_context_reads_like_a_person_not_a_number(self):
        eng = engine()
        eng.state.connection = 0.40
        eng.state.pride = 0.8
        text = eng.prompt_context()
        self.assertIn("拉不下脸", text)
        self.assertNotIn("0.4", text)

    def test_style_guidance_for_swallowed_words(self):
        eng = engine()
        eng.state.connection = 0.40
        eng.state.hesitation = 0.8
        self.assertIn("欲言又止", eng.style_guidance())

    def test_style_guidance_default(self):
        self.assertEqual(engine().style_guidance(), "照常")


class ExtraAxisTests(unittest.TestCase):
    def test_driven_axis_grows_when_its_source_crosses_the_threshold(self):
        eng = engine()
        eng.config = replace(
            eng.config,
            extra_axes=(warmth.AxisSpec(name="执念", kind="driven", source="connection", threshold=0.1, rate=0.01),),
        )
        eng.state.extra["执念"] = 0.0
        eng.state.connection = 0.5
        eng.tick(10)
        self.assertGreater(eng.state.extra["执念"], 0.0)

    def test_regress_axis_returns_to_its_setpoint(self):
        eng = engine()
        eng.config = replace(
            eng.config,
            extra_axes=(warmth.AxisSpec(name="安定", kind="regress", setpoint=0.2, rate=0.05),),
        )
        eng.state.extra["安定"] = 0.9
        eng.tick(10)
        self.assertLess(eng.state.extra["安定"], 0.9)
        self.assertGreaterEqual(eng.state.extra["安定"], 0.2)

    def test_extra_axis_can_be_pushed_from_outside(self):
        eng = engine()
        eng.config = replace(eng.config, extra_axes=(warmth.AxisSpec(name="执念", initial=0.0, low=0.0, high=1.0),))
        eng.state.extra["执念"] = 0.0
        eng.apply_delta(执念=0.4)
        self.assertAlmostEqual(eng.state.extra["执念"], 0.4)


class ConfigTests(unittest.TestCase):
    """人格特化配置：缺文件、缺键、坏值都不能让引擎起不来。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="warmth-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "warmth.json"

    def write(self, payload) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.path.write_text(text, encoding="utf-8")

    def test_missing_file_falls_back_to_defaults_silently(self):
        config, notes = warmth.load_config(path=self.path)
        self.assertEqual(notes, [])
        self.assertEqual(config.thresholds, warmth.Thresholds())
        self.assertEqual(config.rates, warmth.Rates())

    def test_template_round_trips_to_defaults(self):
        warmth.write_template(path=self.path)
        config, notes = warmth.load_config(path=self.path)
        self.assertEqual(notes, [])
        self.assertEqual(config.thresholds, warmth.defaults().thresholds)
        self.assertEqual(config.extra_axes, ())  # 示例只在 _ 开头的键里，不进配置
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(config.rates.accel_delay, written["rates"]["accel_delay"])

    def test_the_template_does_not_pin_every_knob(self):
        """模板写全了就等于把默认值钉死——以后改默认再也影响不到这个文件。"""
        written = warmth.template()
        self.assertLess(len(written["rates"]), len(warmth.Rates().__dataclass_fields__))
        self.assertNotIn("immersion_decay", written["rates"])
        default_rates = warmth.defaults().rates
        for key, value in written["rates"].items():
            self.assertEqual(getattr(default_rates, key), value, key)

    def test_write_template_does_not_clobber_without_overwrite(self):
        self.write({"thresholds": {"observation": 0.9}})
        warmth.write_template(path=self.path)
        config, _ = warmth.load_config(path=self.path)
        self.assertEqual(config.thresholds.observation, 0.9)
        warmth.write_template(path=self.path, overwrite=True)
        config, _ = warmth.load_config(path=self.path)
        self.assertEqual(config.thresholds.observation, 0.20)

    def test_partial_file_keeps_the_rest_default(self):
        self.write({"thresholds": {"consider_contact": 0.42}})
        config, notes = warmth.load_config(path=self.path)
        self.assertEqual(config.thresholds.consider_contact, 0.42)
        self.assertEqual(config.thresholds.force_contact, 0.50)
        self.assertEqual(notes, [])

    def test_unknown_keys_are_reported_and_ignored(self):
        self.write({"thresholds": {"nonsense": 1}, "unknown_group": {"a": 1}})
        config, notes = warmth.load_config(path=self.path)
        self.assertEqual(config.thresholds, warmth.Thresholds())
        self.assertTrue(any("nonsense" in note for note in notes))
        self.assertTrue(any("unknown_group" in note for note in notes))

    def test_bad_values_fall_back_instead_of_crashing(self):
        self.write({"thresholds": {"observation": "很高"}, "rates": {"accel_delay": None}})
        config, notes = warmth.load_config(path=self.path)
        self.assertEqual(config.thresholds.observation, 0.20)
        self.assertEqual(config.rates.accel_delay, 45.0)
        self.assertEqual(len(notes), 2)

    def test_nan_in_config_is_refused(self):
        self.write({"rates": {"connection_base": float("nan")}})
        config, _ = warmth.load_config(path=self.path)
        self.assertEqual(config.rates.connection_base, warmth.Rates().connection_base)

    def test_broken_json_does_not_stop_the_engine(self):
        self.write("{ this is not json")
        config, notes = warmth.load_config(path=self.path)
        self.assertEqual(config.thresholds, warmth.Thresholds())
        self.assertTrue(notes and "读不了" in notes[0])

    def test_a_utf8_bom_is_tolerated(self):
        """Windows 上用记事本或 PowerShell 存盘很容易带 BOM，那不是坏文件。"""
        payload = json.dumps({"thresholds": {"consider_contact": 0.44}}, ensure_ascii=False)
        self.path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
        config, notes = warmth.load_config(path=self.path)
        self.assertEqual(notes, [])
        self.assertAlmostEqual(config.thresholds.consider_contact, 0.44)

    def test_extra_axes_reach_the_engine(self):
        self.write(
            {
                "extra_axes": [
                    {"name": "执念", "kind": "driven", "source": "connection", "threshold": 0.1, "rate": 0.01}
                ]
            }
        )
        config, notes = warmth.load_config(path=self.path)
        self.assertEqual(notes, [])
        self.assertEqual(len(config.extra_axes), 1)
        eng = warmth.Engine(config)
        self.assertIn("执念", eng.state.extra)
        eng.state.connection = 0.5
        eng.tick(10)
        self.assertGreater(eng.state.extra["执念"], 0.0)

    def test_a_nameless_axis_is_reported_not_fatal(self):
        self.write({"extra_axes": [{"kind": "driven"}]})
        config, notes = warmth.load_config(path=self.path)
        self.assertEqual(config.extra_axes, ())
        self.assertTrue(any("缺 name" in note for note in notes))

    def test_custom_user_state_lift_is_respected(self):
        self.write({"user_state_lift": {"busy": 3.0}})
        config, _ = warmth.load_config(path=self.path)
        eng = warmth.Engine(config)
        eng.set_user_state("busy")
        self.assertAlmostEqual(eng.effective_thresholds()["contact"], 0.35 * 3.0)

    def test_config_path_sits_next_to_schedule(self):
        with mock.patch.object(persona, "folder", lambda persona_id=None: self.root):
            self.assertEqual(warmth.config_path("x"), self.root / "memory" / warmth.CONFIG_NAME)

    def test_effective_config_lists_every_knob(self):
        shown = warmth.effective_config(warmth.defaults())
        self.assertEqual(
            set(shown),
            {
                "thresholds",
                "rates",
                "user_state_lift",
                "speak_cooldown_minutes",
                "max_tick_minutes",
                "wake_bonus_minutes",
                "user_state_ttl_minutes",
                "user_state_trigger_ratio",
                "reply_delta",
                "persona",
                "extra_axes",
            },
        )
        self.assertIn("accel_delay", shown["rates"])
        self.assertIn("hesitation_lift", shown["thresholds"])

    def test_every_config_field_can_be_set_from_the_file(self):
        """复核第二轮抓到：max_tick_minutes 加进了 Config 却没接进 load_config，「可配」是空话。

        这条测试是结构性的：**以后往 Config 上加字段而不在这里登记，它会失败。**
        """
        samples = {
            "thresholds": {"observation": 0.3},
            "rates": {"connection_base": 0.001},
            "extra_axes": [{"name": "执念", "rate": 0.01}],
            "user_state_lift": {"busy": 2.0},
            "speak_cooldown_minutes": 30.0,
            "max_tick_minutes": 120.0,
            "wake_bonus_minutes": 15.0,
            "user_state_ttl_minutes": 30.0,
            "user_state_trigger_ratio": 0.9,
            "reply_delta": {"connection": -0.1},
            "persona": {"self_name": "我"},
        }
        self.assertEqual(set(samples), set(warmth.Config.__dataclass_fields__))
        for field_name, value in samples.items():
            with self.subTest(field=field_name):
                self.write({field_name: value})
                config, notes = warmth.load_config(path=self.path)
                self.assertFalse([n for n in notes if "顶层键不认识" in n], notes)
                self.assertNotEqual(getattr(config, field_name), getattr(warmth.defaults(), field_name))

    def test_max_tick_minutes_actually_reaches_the_engine(self):
        self.write({"max_tick_minutes": 120.0})
        config, _ = warmth.load_config(path=self.path)
        eng = warmth.Engine(config)
        eng.tick(180)  # 被 120 截住，但比默认的 60 走得多
        self.assertAlmostEqual(eng.state.connection, 0.0007 * 120, places=6)

    def test_a_silly_max_tick_minutes_is_refused(self):
        for bad in (0, -5, "很久"):
            with self.subTest(bad=bad):
                self.write({"max_tick_minutes": bad})
                config, notes = warmth.load_config(path=self.path)
                self.assertEqual(config.max_tick_minutes, 60.0)
                self.assertTrue(any("max_tick_minutes" in note for note in notes))


class RuntimeTests(unittest.TestCase):
    """空闲 tick：状态落盘、"到点才动"、以及它不去碰记忆。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="warmth-run-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(persona, "folder", lambda persona_id=None: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.root / "memory" / warmth.STATE_NAME

    def snapshot(self) -> dict[str, str]:
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha1(path.read_bytes()).hexdigest()
            for path in sorted(self.root.rglob("*"))
            if path.is_file() and path.name not in {warmth.STATE_NAME, warmth.CONFIG_NAME}
        }

    def test_missing_state_starts_from_zero(self):
        state, notes = warmth.load_state(path=self.state)
        self.assertEqual(notes, [])
        self.assertEqual(state.connection, 0.0)
        self.assertIsNone(state.last_tick)

    def test_state_round_trips(self):
        state = warmth.State(connection=0.4, pride=0.2, hesitation=0.3, user_state="busy")
        state.last_tick = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        warmth.save_state(None, state, path=self.state)
        loaded, _ = warmth.load_state(path=self.state)
        self.assertAlmostEqual(loaded.connection, 0.4)
        self.assertAlmostEqual(loaded.hesitation, 0.3)
        self.assertEqual(loaded.user_state, "busy")
        self.assertEqual(loaded.last_tick, state.last_tick)

    def test_broken_state_falls_back_without_raising(self):
        self.state.write_text("{ not json", encoding="utf-8")
        state, notes = warmth.load_state(path=self.state)
        self.assertEqual(state.connection, 0.0)
        self.assertTrue(notes and "读不了" in notes[0])

    def test_state_with_junk_values_keeps_the_good_ones(self):
        self.state.write_text(json.dumps({"connection": "x", "pride": 5, "valence": float("nan")}), encoding="utf-8")
        state, _ = warmth.load_state(path=self.state)
        self.assertEqual(state.connection, 0.0)
        self.assertEqual(state.pride, 1.0)  # 夹到轴的上界
        self.assertEqual(state.valence, 0.0)

    def test_first_ambient_tick_only_stamps_the_clock(self):
        start = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        result = warmth.ambient_tick(now=start, path=self.state)
        self.assertTrue(result["first"])
        self.assertEqual(result["fired"], [])
        self.assertAlmostEqual(result["snapshot"]["connection"], 0.0)
        state, _ = warmth.load_state(path=self.state)
        self.assertEqual(state.last_tick, start)

    def test_second_ambient_tick_advances_by_the_elapsed_time(self):
        start = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        warmth.ambient_tick(now=start, path=self.state)
        result = warmth.ambient_tick(now=start + timedelta(minutes=30), path=self.state)
        self.assertEqual(result["elapsed"], 30.0)
        self.assertGreater(result["snapshot"]["connection"], 0.0)
        self.assertIn("explain", result)

    def test_a_backwards_clock_changes_nothing(self):
        start = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        warmth.ambient_tick(now=start, path=self.state)
        result = warmth.ambient_tick(now=start - timedelta(minutes=10), path=self.state)
        self.assertEqual(result["elapsed"], 0.0)
        self.assertEqual(result["fired"], [])

    def test_seconds_until_due_walks_from_zero_to_the_interval(self):
        self.assertEqual(warmth.seconds_until_due(), 0.0)  # 还没有状态 = 立刻该跑
        start = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        warmth.ambient_tick(now=start, path=self.state)
        self.assertAlmostEqual(warmth.seconds_until_due(now=start), warmth.tick_minutes() * 60.0)
        self.assertEqual(warmth.seconds_until_due(now=start + timedelta(hours=3)), 0.0)

    def test_tick_minutes_reads_the_real_schedule_file(self):
        """复核第二轮指出：上一版只是 mock 掉 parse_schedule，仍然没有真的打开日程文件。"""
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        (self.root / "memory" / "schedule.txt").write_text(
            "stm_minutes: 5\nroutine_minutes: 30\ntick_minutes: 7\nquota_speak_per_day: 3\n", encoding="utf-8"
        )
        with mock.patch.object(persona, "folder", lambda persona_id=None: self.root):
            self.assertEqual(warmth.tick_minutes("x"), 7.0)
        (self.root / "memory" / "schedule.txt").write_text("stm_minutes: 5\n", encoding="utf-8")
        with mock.patch.object(persona, "folder", lambda persona_id=None: self.root):
            self.assertEqual(warmth.tick_minutes("x"), 20.0)  # 没写就用默认

    def test_ambient_tick_never_touches_memory_files(self):
        (self.root / "memory" / "working.txt").write_text("短期工作台", encoding="utf-8")
        (self.root / "memory" / "semantic").mkdir(exist_ok=True)
        (self.root / "memory" / "semantic" / "facts.txt").write_text("甲 | 乙 | 2026-10-01 | k\n", encoding="utf-8")
        before = self.snapshot()
        start = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        warmth.ambient_tick(now=start, path=self.state)
        warmth.ambient_tick(now=start + timedelta(minutes=45), path=self.state)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(self.state.is_file())


class HeartbeatWiringTests(unittest.TestCase):
    """心跳把积温接上 speak 通道：只有真的要开口才说话。"""

    def run_tick(self, gates: list[str]):
        from app import heartbeat

        fired = [{"gate": gate, "fired": True, "detail": "", "gap": None, "urgency": 0.5} for gate in gates]
        with mock.patch.object(
            heartbeat.warmth,
            "ambient_tick",
            return_value={"elapsed": 30.0, "fired": fired, "hook": "想找对方，但拉不下脸", "style": "欲言又止", "explain": "触发"},
        ), mock.patch.object(heartbeat, "_minutes_since_message", return_value=120.0), mock.patch.object(
            heartbeat.supervisor, "build_packet", return_value={}
        ), mock.patch.object(heartbeat, "deliver_speak", return_value="在。") as speak, mock.patch.object(
            heartbeat, "_log"
        ):
            result = heartbeat.run_warmth_tick("test")
        return result, speak

    def test_contact_goes_through_the_speak_channel(self):
        _, speak = self.run_tick(["contact"])
        self.assertEqual(speak.call_count, 1)
        decision = speak.call_args.args[1]
        self.assertTrue(decision["speak"])
        self.assertIn("拉不下脸", decision["topic"])
        self.assertIn("欲言又止", decision["topic"])

    def test_forced_contact_speaks_too(self):
        _, speak = self.run_tick(["force_contact"])
        self.assertEqual(speak.call_count, 1)

    def test_observation_alone_stays_silent(self):
        _, speak = self.run_tick(["observation"])
        self.assertEqual(speak.call_count, 0)

    def test_a_broken_engine_never_stops_the_heartbeat(self):
        from app import heartbeat

        with mock.patch.object(heartbeat.warmth, "ambient_tick", side_effect=RuntimeError("boom")), mock.patch.object(
            heartbeat, "_log"
        ):
            result = heartbeat.run_warmth_tick("test")
        self.assertEqual(result["fired"], [])
        self.assertIn("boom", result["error"])

    def test_nothing_happens_before_the_interval_elapses(self):
        from app import heartbeat

        with mock.patch.object(
            heartbeat.warmth, "ambient_tick", return_value={"elapsed": 0.0, "fired": []}
        ), mock.patch.object(heartbeat, "_log"), mock.patch.object(heartbeat, "deliver_speak") as speak:
            heartbeat.run_warmth_tick("test")
        self.assertEqual(speak.call_count, 0)


class LedgerTests(unittest.TestCase):
    """台账与人工标注：校准阈值的唯一依据。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="warmth-led-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(persona, "folder", lambda persona_id=None: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.root / "memory" / warmth.STATE_NAME
        self.log = self.root / "memory" / warmth.LOG_NAME
        self.start = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    def test_a_boring_tick_is_not_written_down(self):
        eng = warmth.Engine()
        self.assertFalse(warmth.interesting(eng, []))
        warmth.ambient_tick(now=self.start, path=self.state, log=self.log)
        warmth.ambient_tick(now=self.start + timedelta(minutes=20), path=self.state, log=self.log)
        self.assertFalse(self.log.exists())  # connection 还很浅，没什么可回看的

    def test_a_repeating_gate_is_only_recorded_once(self):
        """复核指出：observation 从 0.20 起每拍都成立，早先会记满 72/72 行。"""
        eng = warmth.Engine()
        eng.state.connection = 0.25
        observation = [{"gate": "observation", "fired": True, "detail": "", "gap": None, "urgency": 0.1}]
        self.assertTrue(warmth.interesting(eng, observation, previous=[]))  # 刚跨进观察带
        first = warmth.gate_set(eng, observation)
        self.assertFalse(warmth.interesting(eng, observation, previous=first))  # 画像没变就不再记

    def test_a_whole_day_records_a_handful_of_lines(self):
        state = warmth.State(connection=0.25)
        state.last_tick = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)
        warmth.save_state(None, state, path=self.state)
        moment = state.last_tick
        for index in range(72):  # 24 小时，20 分钟一拍
            moment = moment + timedelta(minutes=20)
            warmth.ambient_tick(now=moment, minutes_since_message=20 * (index + 1), path=self.state, log=self.log)
        rows = warmth.read_log(path=self.log)
        self.assertLessEqual(len(rows), 12)
        self.assertGreaterEqual(len(rows), 1)

    def test_an_approaching_tick_is_written_down(self):
        eng = warmth.Engine()
        eng.state.connection = eng.effective_thresholds()["contact"] * 0.9
        self.assertTrue(warmth.interesting(eng, []))

    def test_a_fired_tick_is_written_down(self):
        eng = warmth.Engine()
        eng.state.connection = 0.9
        self.assertEqual([entry["gate"] for entry in eng.check() if entry["fired"]][0], "force_contact")
        self.assertTrue(warmth.interesting(eng, [entry for entry in eng.check() if entry["fired"]]))

    def test_an_ambient_tick_logs_its_axes_and_gates(self):
        state = warmth.State(connection=0.45)
        state.last_tick = self.start - timedelta(minutes=30)
        warmth.save_state(None, state, path=self.state)
        result = warmth.ambient_tick(now=self.start, path=self.state, log=self.log)
        self.assertGreaterEqual(result["elapsed"], 30.0)
        self.assertIn("logged", result)
        rows = warmth.read_log(path=self.log)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["kind"], "tick")
        self.assertIn("connection", row)
        self.assertTrue(row["gates"])

    def test_days_filter_keeps_only_recent_rows(self):
        warmth.append_log(
            None,
            {"kind": "tick", "at": (self.start - timedelta(days=9)).isoformat(), "gates": ["observation"]},
            path=self.log,
        )
        warmth.append_log(
            None, {"kind": "tick", "at": self.start.isoformat(), "gates": ["contact"]}, path=self.log
        )
        self.assertEqual(len(warmth.read_log(path=self.log)), 2)
        recent = warmth.read_log(path=self.log, days=3, now=self.start)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["gates"], ["contact"])

    def test_broken_lines_in_the_ledger_are_skipped(self):
        self.log.write_text('{"kind": "tick"}\nnot json\n\n', encoding="utf-8")
        self.assertEqual(len(warmth.read_log(path=self.log)), 1)

    def test_mark_records_the_verdict_and_the_state_at_that_moment(self):
        warmth.save_state(None, warmth.State(connection=0.44, hesitation=0.5), path=self.state)
        entry = warmth.mark(None, "他刚说很难过的事，她该开口", verdict="should_speak", path=self.log, now=self.start)
        self.assertEqual(entry["verdict"], "should_speak")
        self.assertAlmostEqual(entry["connection"], 0.44)
        rows = warmth.read_log(path=self.log)
        self.assertEqual(rows[0]["text"], "他刚说很难过的事，她该开口")

    def test_an_unknown_verdict_degrades_to_unsure(self):
        entry = warmth.mark(None, "说不清", verdict="随便写的", path=self.log, now=self.start)
        self.assertEqual(entry["verdict"], "unsure")

    def test_the_old_verdict_names_still_read(self):
        """P1 第一版用的是 ok_silent / na，老标注不能被判成无效。"""
        self.assertEqual(warmth.mark(None, "a", verdict="ok_silent", path=self.log, now=self.start)["verdict"], "should_stay_silent")
        self.assertEqual(warmth.mark(None, "b", verdict="na", path=self.log, now=self.start)["verdict"], "unsure")

    def test_a_mark_can_say_she_should_not_speak(self):
        entry = warmth.mark(
            None,
            "他在开会，这时候插话很怪",
            verdict="should_stay_silent",
            blame=["threshold", "timing"],
            tags=["白天", "他在忙"],
            path=self.log,
            now=self.start,
        )
        self.assertEqual(entry["verdict"], "should_stay_silent")
        self.assertEqual(entry["blame"], ["threshold", "timing"])
        self.assertEqual(entry["tags"], ["白天", "他在忙"])

    def test_blame_names_are_checked_and_unknown_ones_kept_for_the_author(self):
        entry = warmth.mark(None, "哪根轴不对", blame=["pride", "玄学"], path=self.log, now=self.start)
        self.assertEqual(entry["blame"], ["pride"])
        self.assertEqual(entry["blame_unknown"], ["玄学"])

    def test_expected_gate_is_checked(self):
        self.assertEqual(warmth.mark(None, "a", expected="force_contact", path=self.log, now=self.start)["expected"], "force_contact")
        self.assertIsNone(warmth.mark(None, "b", expected="乱写", path=self.log, now=self.start)["expected"])

    def test_a_mark_records_what_the_engine_thought_at_that_moment(self):
        """数据处理要比的是「引擎的判断」与「人的判断」这一对，缺一半就没法回归。"""
        warmth.save_state(None, warmth.State(connection=0.44, hesitation=0.5), path=self.state)
        entry = warmth.mark(None, "记一笔", path=self.log, now=self.start)
        self.assertIn("engine_gates", entry)
        self.assertIn("engine_explain", entry)
        self.assertIn("cooldown_left", entry)

    def test_stats_summarizes_verdicts_and_blame(self):
        warmth.mark(None, "该开口", verdict="should_speak", blame=["pride"], path=self.log, now=self.start)
        warmth.mark(None, "不该开口", verdict="should_stay_silent", blame=["pride", "threshold"], path=self.log, now=self.start)
        summary = warmth.stats(path=self.log, days=3, now=self.start)
        self.assertEqual(summary["marks"], 2)
        self.assertEqual(summary["verdicts"]["should_speak"], 1)
        self.assertEqual(summary["blame"]["pride"], 2)
        self.assertFalse(summary["ready"])  # 不足 10 条
        text = warmth.render_stats(summary)
        self.assertIn("2 条标注", text)
        self.assertIn("pride 2", text)
        self.assertIn("先别急着调参", text)

    def test_stats_on_an_empty_ledger_is_honest(self):
        summary = warmth.stats(path=self.log, now=self.start)
        self.assertEqual(summary["marks"], 0)
        self.assertIn("0 条标注", warmth.render_stats(summary))

    def test_export_is_json_for_later_processing(self):
        warmth.mark(None, "一条", path=self.log, now=self.start)
        warmth.append_log(None, {"kind": "tick", "at": self.start.isoformat(), "gates": []}, path=self.log)
        payload = json.loads(warmth.export(path=self.log, days=3, now=self.start))
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["rows"][0]["kind"], "mark")

    def test_mark_records_an_explain_that_matches_its_gates(self):
        """复核抓到过的同族问题：gates 是这一刻的、explain 却是上一次 tick 的。"""
        warmth.save_state(None, warmth.State(connection=0.4, pride=0.8, immersion=0.5), path=self.state)
        entry = warmth.mark(None, "记一笔", path=self.log, now=self.start)
        self.assertEqual(entry["engine_gates"], [])
        self.assertIn("blocked", entry["engine_explain"])  # 与 gates 同出一辙

    def test_all_human_facing_output_survives_a_gbk_console(self):
        """这台机器的控制台是 GBK——一个 '⟵' 就让 --review 崩过一次（真事，2026-10-14）。"""
        warmth.mark(
            None, "记一笔", verdict="should_stay_silent", blame=["pride"], tags=["深夜"], path=self.log, now=self.start
        )
        for text in (
            warmth.review(path=self.log, days=3, now=self.start),
            warmth.render_stats(warmth.stats(path=self.log, days=3, now=self.start)),
            warmth.watch_line("x", now=self.start),
        ):
            with self.subTest(text=text[:20]):
                text.encode("gbk")  # 编不出来就抛 UnicodeEncodeError

    def test_review_reports_both_kinds_of_rows(self):
        warmth.append_log(
            None,
            {"kind": "tick", "at": self.start.isoformat(), "connection": 0.4, "pride": 0.6, "valence": -0.1,
             "arousal": 0.2, "immersion": 0.1, "hesitation": 0.3, "gates": ["find_activity"]},
            path=self.log,
        )
        warmth.mark(None, "这里其实该开口", path=self.log, now=self.start)
        text = warmth.review(path=self.log, days=3, now=self.start)
        self.assertIn("1 次值得记的 tick", text)
        self.assertIn("1 条人工标注", text)
        self.assertIn("find_activity", text)
        self.assertIn("这里其实该开口", text)

    def test_review_on_an_empty_ledger_says_so(self):
        self.assertIn("没有记录", warmth.review(path=self.log, now=self.start))

    def test_the_ledger_is_a_new_file_and_memory_is_untouched(self):
        (self.root / "memory" / "working.txt").write_text("短期工作台", encoding="utf-8")
        before = hashlib.sha1((self.root / "memory" / "working.txt").read_bytes()).hexdigest()
        warmth.mark(None, "标注", path=self.log, now=self.start)
        warmth.append_log(None, {"kind": "tick", "at": self.start.isoformat(), "gates": []}, path=self.log)
        after = hashlib.sha1((self.root / "memory" / "working.txt").read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertTrue(self.log.is_file())


class FixTests(unittest.TestCase):
    """首轮自查抓到的四个问题：只涨不落、开口太密、骄傲不防御、两条轴是摆设。"""

    def test_pride_defends_by_default_once_she_starts_missing_him(self):
        eng = warmth.Engine()  # 默认配置：pride_defend_threshold = 0.35
        eng.state.connection = 0.40
        eng.tick(60)
        self.assertGreater(eng.state.pride, 0.0)

    def test_pride_arms_and_then_hesitation_builds(self):
        eng = warmth.Engine()
        eng.state.connection = 0.36
        for _ in range(6):  # 三小时：骄傲爬到防御目标
            eng.tick(30)
        self.assertGreaterEqual(eng.state.pride, 0.49)
        for _ in range(6):  # 再往后：犹豫开始真的累积起来
            eng.tick(30)
        self.assertGreater(eng.state.hesitation, 0.0)

    def test_hesitation_reaches_the_visible_threshold_in_a_long_silence(self):
        eng = warmth.Engine()
        eng.state.connection = 0.36
        for _ in range(40):  # 二十小时没人说话
            eng.tick(30)
        self.assertGreater(eng.state.hesitation, 0.5)
        self.assertIn("欲言又止", eng.style_guidance())

    def test_valence_actually_sinks_while_waiting(self):
        eng = warmth.Engine()
        eng.state.connection = 0.6
        for _ in range(3):
            eng.tick(20)
        self.assertLess(eng.state.valence, -0.05)  # 下沉必须强过回归，否则这条轴是摆设

    def test_arousal_actually_climbs_while_waiting(self):
        eng = warmth.Engine()
        eng.state.connection = 0.45
        for _ in range(15):
            eng.tick(20)
        self.assertGreater(eng.state.arousal, 0.5)

    def test_the_valence_multiplier_can_speed_up_or_slow_down_the_wait(self):
        def grow(**overrides):
            eng = engine(**overrides)
            eng.state.valence = -0.5
            eng.tick(60)
            return eng.state.connection

        plain = grow()
        boosted = grow(rates__valence_connect_boost=1.4, rates__valence_connect_boost_threshold=-0.2)
        dampened = grow(rates__valence_connect_dampen=0.4, rates__valence_connect_dampen_threshold=-0.4)
        self.assertGreater(boosted, plain)
        self.assertLess(dampened, plain)


class CooldownTests(unittest.TestCase):
    """刚开口就不再重复：否则她一跨线就会把当天配额在一小时里烧完。"""

    def test_a_fresh_speak_holds_the_next_one(self):
        eng = warmth.Engine()
        eng.state.connection = 0.60
        eng.state.last_spoke_at = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        eng.tick(1, now=datetime(2026, 10, 14, 12, 5, tzinfo=timezone.utc))
        gates = [entry["gate"] for entry in eng.last_trace if entry["fired"]]
        self.assertNotIn("force_contact", gates)
        held = [entry for entry in eng.last_trace if entry["gate"] == "cooldown"]
        self.assertTrue(held and held[0]["gap"] > 0)

    def test_the_hold_lifts_once_the_cooldown_passes(self):
        eng = warmth.Engine()
        eng.state.connection = 0.60
        eng.state.last_spoke_at = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        eng.tick(1, now=datetime(2026, 10, 14, 12, 50, tzinfo=timezone.utc))
        self.assertIn("force_contact", [entry["gate"] for entry in eng.last_trace if entry["fired"]])

    def test_observation_is_not_held_by_the_cooldown(self):
        eng = warmth.Engine()
        eng.state.connection = 0.25
        eng.state.last_spoke_at = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        eng.tick(1, now=datetime(2026, 10, 14, 12, 5, tzinfo=timezone.utc))
        self.assertIn("observation", [entry["gate"] for entry in eng.last_trace if entry["fired"]])

    def test_a_zero_cooldown_disables_the_hold(self):
        eng = engine()  # 先拿默认，再改 cooldown
        eng.config = replace(eng.config, speak_cooldown_minutes=0.0)
        eng.state.connection = 0.60
        eng.state.last_spoke_at = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        eng.tick(1, now=datetime(2026, 10, 14, 12, 5, tzinfo=timezone.utc))
        self.assertIn("force_contact", [entry["gate"] for entry in eng.last_trace if entry["fired"]])

    def test_note_spoke_is_persisted(self):
        root = Path(tempfile.mkdtemp(prefix="warmth-spoke-"))
        self.addCleanup(shutil.rmtree, root, True)
        path = root / warmth.STATE_NAME
        stamp = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        warmth.note_spoke(None, now=stamp, path=path)
        loaded, _ = warmth.load_state(path=path)
        self.assertEqual(loaded.last_spoke_at, stamp)


class ReplyTests(unittest.TestCase):
    """你回一句，等待就回落——而不是她自己开口就自我安慰。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="warmth-reply-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(persona, "folder", lambda persona_id=None: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.root / "memory" / warmth.STATE_NAME

    def test_a_reply_knocks_connection_down_and_lifts_the_mood(self):
        state = warmth.State(connection=0.60, valence=-0.20, arousal=0.8, hesitation=0.9)
        warmth.save_state(None, state, path=self.state)
        snapshot = warmth.on_user_message(None, path=self.state)
        self.assertAlmostEqual(snapshot["connection"], 0.35)
        self.assertAlmostEqual(snapshot["valence"], -0.15)
        self.assertAlmostEqual(snapshot["arousal"], 0.65)  # 躁动也被安抚
        self.assertAlmostEqual(snapshot["hesitation"], 0.70)  # 不用再咽了
        loaded, _ = warmth.load_state(path=self.state)
        self.assertAlmostEqual(loaded.connection, 0.35)

    def test_a_reply_does_not_stop_the_clock(self):
        stamp = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        state = warmth.State(connection=0.6)
        state.last_tick = stamp
        warmth.save_state(None, state, path=self.state)
        warmth.on_user_message(None, path=self.state)
        loaded, _ = warmth.load_state(path=self.state)
        self.assertEqual(loaded.last_tick, stamp)  # 时间照常流逝

    def test_reply_delta_is_configurable(self):
        self.state.parent.joinpath(warmth.CONFIG_NAME).write_text(
            json.dumps({"reply_delta": {"connection": -0.05}}), encoding="utf-8"
        )
        warmth.save_state(None, warmth.State(connection=0.60), path=self.state)
        snapshot = warmth.on_user_message(None, path=self.state)
        self.assertAlmostEqual(snapshot["connection"], 0.55)

    def test_it_never_drags_connection_below_zero(self):
        warmth.save_state(None, warmth.State(connection=0.1), path=self.state)
        snapshot = warmth.on_user_message(None, path=self.state)
        self.assertEqual(snapshot["connection"], 0.0)


class UserStateTests(unittest.TestCase):
    """他忙不忙：由模型推断，但只在真能改变结论的时候才问。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="warmth-user-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(persona, "folder", lambda persona_id=None: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.root / "memory" / warmth.STATE_NAME
        self.start = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    def test_setting_a_state_stamps_the_time(self):
        warmth.set_user_state(None, "busy", now=self.start, path=self.state)
        loaded, _ = warmth.load_state(path=self.state)
        self.assertEqual(loaded.user_state, "busy")
        self.assertEqual(loaded.user_state_at, self.start)

    def test_an_unknown_state_is_ignored(self):
        warmth.set_user_state(None, "很忙", now=self.start, path=self.state)
        loaded, _ = warmth.load_state(path=self.state)
        self.assertEqual(loaded.user_state, "active")
        self.assertIsNone(loaded.user_state_at)

    def test_a_fresh_guess_is_not_asked_again(self):
        warmth.set_user_state(None, "active", now=self.start, path=self.state)
        state, _ = warmth.load_state(path=self.state)
        state.connection = 0.5  # 已经在开口线附近
        warmth.save_state(None, state, path=self.state)
        self.assertFalse(warmth.needs_user_state(None, now=self.start + timedelta(minutes=30), path=self.state))

    def test_a_stale_guess_is_renewed_only_when_it_matters(self):
        warmth.set_user_state(None, "active", now=self.start, path=self.state)
        state, _ = warmth.load_state(path=self.state)
        state.connection = 0.5
        warmth.save_state(None, state, path=self.state)
        later = self.start + timedelta(hours=8)
        self.assertTrue(warmth.needs_user_state(None, now=later, path=self.state))

        state.connection = 0.05  # 离开口线还远，问也没用
        warmth.save_state(None, state, path=self.state)
        self.assertFalse(warmth.needs_user_state(None, now=later, path=self.state))

    def test_backlog_maps_to_an_activity(self):
        self.assertIsNone(warmth.activity_for_backlog(0))
        self.assertEqual(warmth.activity_for_backlog(1), "observe")
        self.assertEqual(warmth.activity_for_backlog(4), "browse")
        self.assertEqual(warmth.activity_for_backlog(12), "reading")

    def test_backlog_count_reads_the_learn_queue(self):
        self.assertEqual(warmth.backlog_count(None), 0)  # 还没有队列文件
        queue = self.root / "memory" / "learn_queue"
        queue.mkdir(parents=True)
        path = queue / "pending.jsonl"
        path.write_text('{"claim": "a"}\n\n{"claim": "b"}\n', encoding="utf-8")
        self.assertEqual(warmth.backlog_count(None), 2)

    def test_a_dormant_backlog_does_not_count_as_being_busy(self):
        """积压是状态，不是活动：放了很久的待办不该让她永远"埋头"。

        注意：文件时间要**相对假 now** 来设，不能拿真实时间减——否则每到新的一天这条就会假失败。
        """
        queue = self.root / "memory" / "learn_queue"
        queue.mkdir(parents=True)
        path = queue / "pending.jsonl"
        path.write_text('{"claim": "a"}\n', encoding="utf-8")
        old = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        ten_hours_ago = (old - timedelta(hours=10)).timestamp()
        os.utime(path, (ten_hours_ago, ten_hours_ago))
        self.assertEqual(warmth.backlog_count(None, fresh_within_minutes=120, now=old), 0)
        self.assertEqual(warmth.backlog_count(None), 1)  # 不传窗口就只看条数

    def test_a_backlog_slows_the_growth_and_occupies_her(self):
        state = warmth.State()
        state.last_tick = self.start
        warmth.save_state(None, state, path=self.state)
        busy = warmth.ambient_tick(now=self.start + timedelta(minutes=60), path=self.state, backlog=12)

        warmth.save_state(None, warmth.State(last_tick=self.start), path=self.state)
        idle = warmth.ambient_tick(now=self.start + timedelta(minutes=60), path=self.state, backlog=0)

        self.assertLess(busy["snapshot"]["connection"], idle["snapshot"]["connection"])
        self.assertGreater(busy["snapshot"]["immersion"], 0.0)
        self.assertEqual(idle["snapshot"]["immersion"], 0.0)

    def test_a_busy_user_raises_the_bar(self):
        state = warmth.State(connection=0.40, user_state="busy")
        warmth.save_state(None, state, path=self.state)
        data = warmth.view(None, now=self.start)
        self.assertGreater(data["thresholds"]["contact"], 0.35)


class ViewTests(unittest.TestCase):
    """给人看的入口：--view / --watch / /api/warmth。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="warmth-view-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(persona, "folder", lambda persona_id=None: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.start = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    def test_view_reports_every_axis_and_gate(self):
        data = warmth.view("x", now=self.start)
        self.assertEqual(
            set(data["axes"]), {"connection", "pride", "valence", "arousal", "immersion", "hesitation"}
        )
        self.assertIn("contact", data["thresholds"])
        self.assertTrue(data["gates"])
        self.assertIn("explain", data)
        self.assertIn("tick_minutes", data)

    def test_view_is_read_only(self):
        before = sorted(path.name for path in (self.root / "memory").iterdir())
        warmth.view("x", now=self.start)
        self.assertEqual(before, sorted(path.name for path in (self.root / "memory").iterdir()))

    def test_watch_line_is_one_readable_line(self):
        line = warmth.watch_line("x", now=self.start)
        self.assertEqual(line.count("\n"), 0)
        self.assertIn("c ", line)
        self.assertIn("积压", line)

    def test_the_bar_reflects_the_value(self):
        self.assertEqual(warmth._bar(0.0, 0, 1, width=4), "····")
        self.assertEqual(warmth._bar(1.0, 0, 1, width=4), "████")
        self.assertEqual(warmth._bar(0.0, -1, 1, width=2), "█·")


class ReviewFixTests(unittest.TestCase):
    """2026-10-14 复核（Cursor）指出的问题，逐条留一条测试。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="warmth-rv-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(persona, "folder", lambda persona_id=None: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.root / "memory" / warmth.STATE_NAME
        self.log = self.root / "memory" / warmth.LOG_NAME
        self.start = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    def test_stats_survives_a_mark_that_records_the_gap(self):
        """复核：`sum(bucket.pop(...)) / len(bucket[...])` 会在同一行 KeyError——一标真数据就崩。"""
        warmth.append_log(
            None,
            {
                "kind": "mark",
                "at": self.start.isoformat(),
                "verdict": "should_speak",
                "minutes_since_message": 80,
                "connection": 0.4,
            },
            path=self.log,
        )
        summary = warmth.stats(path=self.log, days=3, now=self.start)
        self.assertEqual(summary["marks"], 1)
        self.assertEqual(summary["by_verdict"]["should_speak"]["since_message_mean"], 80.0)

    def test_old_verdict_names_are_normalized_when_read(self):
        """复核：别名只在写入时映射过，台账里存着的 `ok_silent` 读出来还是它。"""
        warmth.append_log(None, {"kind": "mark", "at": self.start.isoformat(), "verdict": "ok_silent"}, path=self.log)
        summary = warmth.stats(path=self.log, days=3, now=self.start)
        self.assertEqual(summary["verdicts"]["should_stay_silent"], 1)
        self.assertEqual(summary["verdicts"].get("ok_silent", 0), 0)  # 旧名不该被单列成一个结论

    def test_the_cooldown_counts_down_with_the_real_clock(self):
        """复核：面板每 3 秒刷新，这个数却要等下一拍才动。"""
        eng = warmth.Engine()
        eng.state.last_spoke_at = self.start
        eng.state.last_tick = self.start + timedelta(minutes=40)
        self.assertAlmostEqual(eng.cooldown_left(), 5.0)  # 按这一拍算（闸门用）
        self.assertAlmostEqual(eng.cooldown_left(self.start + timedelta(minutes=15)), 30.0)  # 按真实现在算（显示用）

    def test_the_view_and_the_mark_agree_about_the_cooldown(self):
        """复核第二轮：`view()` 用的是现在，`mark()` 记下的却按上一拍算——同一时刻两个数。

        实测过 50 分钟那一档：界面 0.0 触发 `force_contact`，标注里却写着冷却 45.0。
        """
        for minutes, expected in ((30.0, 15.0), (50.0, 0.0)):
            with self.subTest(minutes=minutes):
                state = warmth.State(connection=0.80, last_spoke_at=self.start, last_tick=self.start)
                warmth.save_state(None, state, path=self.state)
                moment = self.start + timedelta(minutes=minutes)
                shown = warmth.view("x", now=moment)
                entry = warmth.mark("x", "probe", now=moment, path=self.log)
                self.assertAlmostEqual(shown["cooldown_left"], expected)
                self.assertAlmostEqual(entry["cooldown_left"], expected)
                self.assertEqual(entry["cooldown_left"], shown["cooldown_left"])
                # 冷却没过完时，强制开口应当被挡住——两个视角必须一致
                self.assertEqual(entry["engine_gates"], [g["gate"] for g in shown["gates"] if g["fired"]])

    def test_the_inner_voice_respects_the_lifted_thresholds(self):
        """复核：sleeping 把强制线抬到 1.0，内心话却还说「已经压不住了」。"""
        eng = warmth.Engine()
        eng.state.connection = 0.55
        eng.state.user_state = "sleeping"
        self.assertEqual([e["gate"] for e in eng.check() if e["fired"]], ["observation"])
        self.assertEqual(eng.prompt_context(), "偶尔会想起对方")

    def test_the_view_reports_fresh_and_total_backlog(self):
        """复核：引擎按"新鲜"算，界面显示总数——同一屏上两个数会打架。"""
        queue = self.root / "memory" / "learn_queue"
        queue.mkdir(parents=True)
        path = queue / "pending.jsonl"
        path.write_text('{"claim": "a"}\n', encoding="utf-8")
        # 相对假 now 拨时间（拿真实时间减会在跨天后假失败）
        ten_hours_ago = (self.start - timedelta(hours=10)).timestamp()
        os.utime(path, (ten_hours_ago, ten_hours_ago))
        data = warmth.view("x", now=self.start)
        self.assertEqual(data["backlog"], 0)  # 引擎用的：新鲜的
        self.assertEqual(data["backlog_total"], 1)  # 给人看的：总数

    def test_undoing_a_reply_puts_the_waiting_back(self):
        """复核：发送失败会撤回消息，可积温的回落留在那里了。"""
        warmth.save_state(None, warmth.State(connection=0.60, valence=-0.20), path=self.state)
        warmth.on_user_message(None, path=self.state)
        back = warmth.undo_user_message(None, path=self.state)
        self.assertAlmostEqual(back["connection"], 0.60)
        self.assertAlmostEqual(back["valence"], -0.20)

    def test_the_template_exposes_the_valence_multiplier(self):
        """复核：记录说「写进模板」，`template()` 里其实没有这两个键。"""
        rates = warmth.template()["rates"]
        for key in (
            "valence_connect_boost",
            "valence_connect_boost_threshold",
            "valence_connect_dampen",
            "valence_connect_dampen_threshold",
        ):
            with self.subTest(key=key):
                self.assertIn(key, rates)

    def test_a_failed_guess_does_not_leave_a_stale_user_state(self):
        """复核：推断抛错只打日志，过期的 sleeping 会一直抬着门槛。"""
        from app import heartbeat

        with mock.patch.object(heartbeat.warmth, "needs_user_state", return_value=True), mock.patch.object(
            heartbeat, "_infer_user_state", side_effect=RuntimeError("boom")
        ), mock.patch.object(heartbeat.warmth, "set_user_state") as setter, mock.patch.object(
            heartbeat.warmth, "ambient_tick", return_value={"elapsed": 0.0, "fired": []}
        ), mock.patch.object(heartbeat, "_log"):
            heartbeat.run_warmth_tick("test")
        setter.assert_called_once_with("test", "active")


class PresenceTests(unittest.TestCase):
    """确定性判断"他在不在"——人报过：她先开口，然后才可能知道对方在不在。

    以前完全没有暂离判断；现在先问免费的在场信号，问不出来才花模型调用。
    """

    def setUp(self):
        from app import heartbeat

        self.heartbeat = heartbeat
        self.root = Path(tempfile.mkdtemp(prefix="warmth-presence-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.object(heartbeat.files, "TRANSCRIPT_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        schedule = mock.patch.object(heartbeat.supervisor, "parse_schedule", return_value={"stm_minutes": 5})
        schedule.start()
        self.addCleanup(schedule.stop)

    def turns(self, *rows: tuple[str, float]) -> None:
        path = self.root / "x" / "2026-10-15.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc)
        lines = [
            json.dumps({"role": role, "created_at": (stamp - timedelta(minutes=ago)).isoformat(), "content": "x"})
            for role, ago in rows
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_a_fresh_turn_from_him_means_he_is_there(self):
        self.turns(("assistant", 30), ("user", 2))
        self.assertEqual(self.heartbeat._presence("x"), "active")

    def test_silence_after_her_turn_means_he_walked_away(self):
        self.turns(("user", 40), ("assistant", 20))
        self.assertEqual(self.heartbeat._presence("x"), "away")

    def test_a_brief_gap_is_left_to_the_model(self):
        self.turns(("user", 40), ("assistant", 5))
        self.assertIsNone(self.heartbeat._presence("x"))

    def test_no_transcript_at_all_is_left_to_the_model(self):
        self.assertIsNone(self.heartbeat._presence("x"))

    def test_the_last_turn_reports_who_and_how_long(self):
        self.turns(("user", 30), ("assistant", 12))
        role, gap = self.heartbeat._last_turn("x")
        self.assertEqual(role, "assistant")
        self.assertAlmostEqual(gap, 12.0, delta=0.5)

    def test_the_presence_signal_is_asked_before_the_model(self):
        heartbeat = self.heartbeat
        with mock.patch.object(heartbeat.warmth, "needs_user_state", return_value=True), mock.patch.object(
            heartbeat, "_presence", return_value="away"
        ), mock.patch.object(heartbeat, "_infer_user_state") as model, mock.patch.object(
            heartbeat.warmth, "set_user_state"
        ) as setter, mock.patch.object(
            heartbeat.warmth, "ambient_tick", return_value={"elapsed": 0.0, "fired": []}
        ), mock.patch.object(heartbeat, "_log"):
            heartbeat.run_warmth_tick("x")
        model.assert_not_called()  # 免费的信号先问
        setter.assert_called_once_with("x", "away")

    def test_the_model_is_used_when_presence_cannot_tell(self):
        heartbeat = self.heartbeat
        with mock.patch.object(heartbeat.warmth, "needs_user_state", return_value=True), mock.patch.object(
            heartbeat, "_presence", return_value=None
        ), mock.patch.object(heartbeat, "_infer_user_state", return_value="busy") as model, mock.patch.object(
            heartbeat.warmth, "set_user_state"
        ) as setter, mock.patch.object(
            heartbeat.warmth, "ambient_tick", return_value={"elapsed": 0.0, "fired": []}
        ), mock.patch.object(heartbeat, "_log"):
            heartbeat.run_warmth_tick("x")
        model.assert_called_once()
        setter.assert_called_once_with("x", "busy")


class DeltaTests(unittest.TestCase):
    """对话 → 六轴的一次性注入：她是被这一轮推动的，还是自己长出来的。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="warmth-delta-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(persona, "folder", lambda persona_id=None: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.root / "memory" / warmth.STATE_NAME
        self.log = self.root / "memory" / warmth.LOG_NAME
        self.start = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    # ── 解析：模型爱说话，抽不出来就当没发生 ──
    def test_a_clean_json_object_is_read(self):
        self.assertEqual(warmth.parse_deltas('{"pride": 0.2}'), {"pride": 0.2})

    def test_json_wrapped_in_prose_is_still_read(self):
        self.assertEqual(warmth.parse_deltas('好的：{"valence": -0.15} 就这样'), {"valence": -0.15})

    def test_garbage_and_unknown_axes_are_dropped(self):
        self.assertEqual(warmth.parse_deltas("我看不太出来"), {})
        self.assertEqual(warmth.parse_deltas('{"nonsense": 1, "pride": "很高"}'), {})
        self.assertEqual(warmth.parse_deltas('{"pride": NaN}'), {})
        self.assertEqual(warmth.parse_deltas('{"pride": 0}'), {})  # 0 等于没变

    # ── 应用：夹紧、只认认识的轴、失败不动 ──
    def test_the_delta_is_clamped_per_axis(self):
        warmth.save_state(None, warmth.State(), path=self.state)
        result = warmth.apply_conversation(None, {"pride": 5.0, "connection": -9.0}, path=self.state, log=self.log)
        self.assertEqual(result["applied"]["pride"], 0.3)  # 参照实现的幅度上限
        self.assertEqual(result["applied"]["connection"], -0.5)
        loaded, _ = warmth.load_state(path=self.state)
        self.assertAlmostEqual(loaded.pride, 0.3)

    def test_an_empty_or_bad_delta_changes_nothing(self):
        warmth.save_state(None, warmth.State(valence=0.5), path=self.state)
        self.assertFalse(warmth.apply_conversation(None, {}, path=self.state, log=self.log)["changed"])
        self.assertFalse(warmth.apply_conversation(None, {"谁": 1}, path=self.state, log=self.log)["changed"])
        loaded, _ = warmth.load_state(path=self.state)
        self.assertAlmostEqual(loaded.valence, 0.5)

    def test_only_a_noticeable_turn_reaches_the_ledger(self):
        warmth.save_state(None, warmth.State(), path=self.state)
        warmth.apply_conversation(None, {"pride": 0.02}, path=self.state, log=self.log)  # 太轻，不记
        self.assertFalse(self.log.exists())
        warmth.apply_conversation(None, {"pride": -0.25}, source="chat", path=self.state, log=self.log)
        rows = warmth.read_log(path=self.log)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "delta")
        self.assertEqual(rows[0]["source"], "chat")

    def test_the_source_tag_survives(self):
        warmth.save_state(None, warmth.State(), path=self.state)
        warmth.apply_conversation(None, {"valence": 0.3}, source="inspiration", note="想到个好玩的", path=self.state, log=self.log)
        self.assertEqual(warmth.read_log(path=self.log)[0]["source"], "inspiration")

    def test_an_unknown_source_degrades_to_chat(self):
        warmth.save_state(None, warmth.State(), path=self.state)
        warmth.apply_conversation(None, {"valence": 0.3}, source="瞎写", path=self.state, log=self.log)
        self.assertEqual(warmth.read_log(path=self.log)[0]["source"], "chat")

    def test_review_counts_injections(self):
        warmth.save_state(None, warmth.State(), path=self.state)
        warmth.apply_conversation(None, {"pride": 0.3}, path=self.state, log=self.log, now=self.start)
        text = warmth.review(None, days=1, path=self.log, now=self.start)
        self.assertIn("1 次明显的情绪注入", text)
        self.assertIn("pride +0.30", text)


class WakeBonusTests(unittest.TestCase):
    """关机不涨，但开机那一下补一点——人定的是 60 分钟。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="warmth-wake-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(persona, "folder", lambda persona_id=None: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.root / "memory" / warmth.STATE_NAME
        self.start = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    def run_gap(self, minutes: float) -> dict:
        state = warmth.State()
        state.last_tick = self.start - timedelta(minutes=minutes)
        warmth.save_state(None, state, path=self.state)
        return warmth.ambient_tick(now=self.start, path=self.state)

    def test_the_shipped_default_is_sixty_minutes(self):
        self.assertEqual(warmth.defaults().wake_bonus_minutes, 60.0)

    def test_a_long_gap_gets_the_bonus_but_a_short_one_does_not(self):
        long_gap = self.run_gap(300)  # 关机 5 小时
        self.assertTrue(long_gap["capped"])
        self.assertAlmostEqual(long_gap["applied"], 120.0)  # 60 上限 + 60 补偿
        self.assertAlmostEqual(long_gap["wake_bonus"], 60.0)

        short_gap = self.run_gap(30)  # 一直在跑
        self.assertFalse(short_gap["capped"])
        self.assertAlmostEqual(short_gap["applied"], 30.0)
        self.assertAlmostEqual(short_gap["wake_bonus"], 0.0)

    def test_the_bonus_is_configurable_and_can_be_switched_off(self):
        config = warmth.defaults()
        config = replace(config, wake_bonus_minutes=0.0)
        state = warmth.State()
        state.last_tick = self.start - timedelta(hours=5)
        warmth.save_state(None, state, path=self.state)
        result = warmth.ambient_tick(now=self.start, config=config, path=self.state)
        self.assertAlmostEqual(result["applied"], 60.0)
        self.assertAlmostEqual(result["wake_bonus"], 0.0)


if __name__ == "__main__":
    unittest.main()
