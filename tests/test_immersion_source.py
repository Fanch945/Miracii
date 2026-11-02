"""沉浸度的来源：学习队列**不是**她的活动（人报："翻开应用就自主高沉浸"）。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import persona, warmth


class ImmersionSourceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="warmth-src-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(persona, "folder", lambda persona_id=None: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = self.root / "memory" / warmth.STATE_NAME
        self.log = self.root / "memory" / warmth.LOG_NAME
        self.start = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)

    def run_tick(self, backlog: int, config=None) -> float:
        warmth.save_state(None, warmth.State(last_tick=self.start - timedelta(minutes=10)), path=self.state)
        warmth.ambient_tick(now=self.start, path=self.state, backlog=backlog, config=config, log=self.log)
        return warmth.load_state(path=self.state)[0].immersion

    def test_the_shipped_default_ignores_the_learn_queue(self):
        self.assertEqual(warmth.defaults().immersion_from_backlog, False)
        self.assertEqual(self.run_tick(12), 0.0)  # 队里有 12 条，也不再当她"在忙"

    def test_the_experiment_is_still_reachable_but_off_by_default(self):
        config = replace(warmth.defaults(), immersion_from_backlog=True)
        self.assertGreater(self.run_tick(12, config=config), 0.0)

    def test_a_conversation_is_the_only_current_source(self):
        warmth.save_state(None, warmth.State(last_tick=self.start), path=self.state)
        warmth.apply_conversation(None, {"immersion": 0.2}, path=self.state, log=self.log, now=self.start)
        self.assertGreater(warmth.load_state(path=self.state)[0].immersion, 0.0)

    def test_the_flag_can_be_set_from_the_config_file(self):
        path = self.root / "memory" / warmth.CONFIG_NAME
        path.write_text('{"immersion_from_backlog": true}', encoding="utf-8")
        config, notes = warmth.load_config(None)
        self.assertEqual(notes, [])
        self.assertTrue(config.immersion_from_backlog)


if __name__ == "__main__":
    unittest.main()
