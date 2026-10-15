"""复核（Cursor / 2026-10-16）在对话注入那条记录里抓到的三处，逐条留个守卫。

1. 状态文件"读—改—写"没有锁：心跳 tick 与对话判线程同时写，**后写的会盖掉刚注入的那一笔**。
2. `hesitation` 的注入下限是 −0.5，而记录与提示词都写"其余轴 ±0.3"。
3. `apply_conversation` 返回值里的 `source` 没归一化：台账写 `chat`、调用方拿到原字符串。
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import persona, warmth


class InjectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="warmth-race-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(persona, "folder", lambda persona_id=None: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.root / "memory" / warmth.STATE_NAME
        self.log = self.root / "memory" / warmth.LOG_NAME
        self.start = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

    def state_now(self) -> warmth.State:
        loaded, _ = warmth.load_state(path=self.state)
        return loaded

    def test_hesitation_cannot_be_driven_below_the_documented_floor(self):
        warmth.save_state(None, warmth.State(hesitation=0.8), path=self.state)
        warmth.apply_conversation(None, {"hesitation": -0.9}, path=self.state, log=self.log)
        self.assertAlmostEqual(self.state_now().hesitation, 0.5)  # 0.8 − 0.3，不是 0.8 − 0.5

    def test_only_connection_may_go_to_negative_half(self):
        warmth.save_state(None, warmth.State(connection=0.6), path=self.state)
        warmth.apply_conversation(None, {"connection": -9.0}, path=self.state, log=self.log)
        self.assertAlmostEqual(self.state_now().connection, 0.1)  # 0.6 − 0.5

    def test_the_returned_source_matches_what_was_written(self):
        warmth.save_state(None, warmth.State(), path=self.state)
        result = warmth.apply_conversation(None, {"pride": 0.3}, source="瞎写", path=self.state, log=self.log)
        self.assertEqual(result["source"], "chat")
        self.assertEqual(warmth.read_log(path=self.log)[0]["source"], "chat")

    def test_an_injection_is_not_lost_when_a_tick_writes_at_the_same_time(self):
        """没有锁时这条会挂：tick 用旧快照整份写回，把注入的 pride 抹成 0。"""
        warmth.save_state(None, warmth.State(last_tick=self.start), path=self.state)
        errors: list[Exception] = []
        moment = self.start + timedelta(minutes=20)

        def inject() -> None:
            try:
                warmth.apply_conversation(None, {"pride": 0.3}, path=self.state, log=self.log)
            except Exception as exc:  # pragma: no cover - 出错要看得见
                errors.append(exc)

        def tick() -> None:
            try:
                warmth.ambient_tick(now=moment, path=self.state, log=self.log)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=inject) for _ in range(6)]
        threads += [threading.Thread(target=tick) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertGreater(self.state_now().pride, 0.0)  # 注入活下来了
        self.assertGreater(self.state_now().connection, 0.0)  # tick 的推进也在


class DeltaDocTests(unittest.TestCase):
    """文档与实现必须说同一件事——复核就是靠这一条抓到 hesitation 那个不一致的。"""

    def test_the_caps_table_matches_the_judge_prompt(self):
        from app import heartbeat

        for name, (low, high) in warmth.CONVERSATION_CAPS.items():
            if name == "connection":
                self.assertEqual((low, high), (-0.5, 0.3))
            else:
                self.assertEqual((low, high), (-0.3, 0.3), name)
        self.assertIn("connection 最低可到 -0.5", heartbeat._JUDGE_PROMPT)

    def test_the_knowledge_doc_states_the_same_caps(self):
        import re

        text = Path("docs/WARMTH.txt").read_text(encoding="utf-8")
        self.assertIn("connection −0.5~+0.3", text)
        self.assertIn("其余 `±0.3`", text)


if __name__ == "__main__":
    unittest.main()
