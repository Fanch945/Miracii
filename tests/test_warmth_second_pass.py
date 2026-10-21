"""第二轮优化（人的实机反馈）：valence 静止点、immersion 被抢写、user_state 可追溯。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import persona, warmth


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="warmth-2nd-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(persona, "folder", lambda persona_id=None: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.root / "memory" / warmth.STATE_NAME
        self.log = self.root / "memory" / warmth.LOG_NAME
        self.start = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


class ValenceTests(Base):
    """人反馈："愉悦度降得太快，负值成了常态"。

    根因：早先"先回归到 0、再下沉一整拍"，静止点恰好等于一拍的下沉量。
    现在两个力合成净速率，静止点由 regress 与 drift 的平衡点决定。
    """

    def test_low_connection_returns_to_calm_not_negative(self):
        eng = warmth.Engine()
        eng.state.connection = 0.10  # 远低于平衡点（regress 0.005 / drift 0.012 ≈ 0.42）
        eng.state.valence = -0.15
        eng.tick(60)  # 净速率 = 0.005 − 0.0012 = +0.0038/分钟 → 一小时足以回到 0
        self.assertAlmostEqual(eng.state.valence, 0.0, places=6)

    def test_high_connection_still_sinks_but_smoothly(self):
        eng = warmth.Engine()
        eng.state.connection = 1.0
        eng.state.valence = 0.0
        eng.tick(60)
        # 净速率 = 0.005 − 0.012×1.0 = −0.007/分钟 → 一小时约 −0.42
        self.assertAlmostEqual(eng.state.valence, -0.42, delta=0.02)

    def test_a_reply_lifts_the_mood_meaningfully(self):
        state = warmth.State(connection=0.5, valence=-0.30)
        warmth.save_state(None, state, path=self.state)
        after = warmth.on_user_message(None, path=self.state)
        self.assertGreater(after["valence"], -0.30)

    def test_the_band_is_readable(self):
        labels = [label for _, label in warmth.VALENCE_BANDS]
        self.assertEqual(warmth.valence_band(0.5), labels[0])
        self.assertEqual(warmth.valence_band(0.0), labels[2])
        self.assertEqual(warmth.valence_band(-0.3), labels[3])
        self.assertEqual(warmth.valence_band(-0.6), labels[4])
        self.assertEqual(warmth.valence_band(-0.99), labels[5])


class ImmersionTests(Base):
    """人反馈："两天前晚上有一段高沉浸，今天的沉浸又与"在思考一个模型"一致吗"。

    实测：台账里 tick 行的 i 全是 0，delta 行在涨——**沉浸被心跳清零了**。
    """

    def test_a_tick_without_backlog_does_not_wipe_an_injected_immersion(self):
        warmth.save_state(None, warmth.State(last_tick=self.start), path=self.state)
        warmth.apply_conversation(None, {"immersion": 0.3}, path=self.state, log=self.log, now=self.start)
        self.assertGreater(warmth.load_state(path=self.state)[0].immersion, 0.0)
        warmth.ambient_tick(now=self.start + timedelta(minutes=20), path=self.state, backlog=0, log=self.log)
        self.assertGreater(warmth.load_state(path=self.state)[0].immersion, 0.0)

    def test_an_injected_immersion_decays_on_its_own(self):
        warmth.save_state(None, warmth.State(last_tick=self.start), path=self.state)
        warmth.apply_conversation(None, {"immersion": 0.3}, path=self.state, log=self.log, now=self.start)
        warmth.ambient_tick(now=self.start + timedelta(minutes=10), path=self.state, backlog=0, log=self.log)
        risen = warmth.load_state(path=self.state)[0].immersion
        self.assertLess(risen, 0.3)  # 按 immersion_decay 自然退去
        self.assertGreater(risen, 0.0)

    def test_a_real_backlog_still_occupies_her(self):
        warmth.save_state(None, warmth.State(last_tick=self.start), path=self.state)
        warmth.ambient_tick(now=self.start + timedelta(minutes=20), path=self.state, backlog=12, log=self.log)
        self.assertGreater(warmth.load_state(path=self.state)[0].immersion, 0.0)


class UserStateHistoryTests(Base):
    """人反馈："历史里查不到 user_state 什么时候、为什么变"。"""

    def test_a_change_is_written_down_with_its_source(self):
        warmth.set_user_state(None, "busy", source="model", note="模型判断", path=self.state, log=self.log, now=self.start)
        rows = warmth.read_log(path=self.log)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "state")
        self.assertEqual(rows[0]["user_state"], "busy")
        self.assertEqual(rows[0]["source"], "model")

    def test_the_same_value_is_not_written_again(self):
        warmth.set_user_state(None, "away", source="presence", path=self.state, log=self.log, now=self.start)
        warmth.set_user_state(None, "away", source="presence", path=self.state, log=self.log, now=self.start)
        self.assertEqual(len(warmth.read_log(path=self.log)), 1)

    def test_an_unknown_value_changes_nothing(self):
        warmth.set_user_state(None, "很忙", path=self.state, log=self.log, now=self.start)
        self.assertEqual(warmth.load_state(path=self.state)[0].user_state, "active")
        self.assertFalse(self.log.exists())


class BacklogCommentTests(Base):
    """学习队列的头两行是注释；把它们当成待办会让档位虚高一档。"""

    def test_comment_lines_are_not_counted(self):
        queue = self.root / "memory" / "learn_queue"
        queue.mkdir(parents=True)
        (queue / "pending.jsonl").write_text(
            "# 每行一条 JSON\n# 示例：{}\n{\"claim\": \"a\"}\n{\"claim\": \"b\"}\n", encoding="utf-8"
        )
        self.assertEqual(warmth.backlog_count(None), 2)


class SecondReviewFixTests(Base):
    """复核（2026-10-21）抓到的两处语义。"""

    def test_above_baseline_both_forces_pull_down(self):
        eng = warmth.Engine()
        eng.state.connection = 1.0
        eng.state.valence = 0.50
        eng.tick(60, catch_up_minutes=0)
        # 回归朝下 + 下沉也朝下：(0.005 + 0.012) × 60 = 1.02 → 夹到基线 0
        self.assertAlmostEqual(eng.state.valence, 0.0, places=6)

    def test_a_backlog_is_a_floor_not_a_ceiling_for_immersion(self):
        # 单次注入上限是 ±0.3，0.85 这种值要靠多轮累积；这里直接造出那个状态
        warmth.save_state(None, warmth.State(immersion=0.85, last_tick=self.start), path=self.state)
        warmth.ambient_tick(now=self.start + timedelta(minutes=1), path=self.state, backlog=12, log=self.log)
        self.assertAlmostEqual(warmth.load_state(path=self.state)[0].immersion, 0.85, delta=0.02)

    def test_a_backlog_still_raises_a_low_immersion(self):
        warmth.save_state(None, warmth.State(immersion=0.0, last_tick=self.start), path=self.state)
        warmth.ambient_tick(now=self.start + timedelta(minutes=1), path=self.state, backlog=12, log=self.log)
        self.assertAlmostEqual(warmth.load_state(path=self.state)[0].immersion, 0.6, delta=0.02)


if __name__ == "__main__":


    unittest.main()
