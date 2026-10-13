"""积温引擎：确定性、漂移、闸门、犹豫与 user_state 的调制。

全部在内存里跑，不碰记忆，也不写盘。
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app import warmth


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


if __name__ == "__main__":
    unittest.main()
