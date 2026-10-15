"""巩固阶段的时序：**正在聊的时候不要抢跑**。

人实测报过：晚上 11 点那轮对话，短期窗口还没到，就被 23:00 的 daily 折进长期记忆了。
根因是 `due_phase` 里 `missed_daily` 的检查落在 `if not live:` 之外。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import clock


class DuePhaseTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 15, 23, 30, tzinfo=timezone.utc)  # 刚过 23:00

    def state(self, chat_minutes_ago: float | None) -> dict:
        return {
            "last_chat_at": (self.now - timedelta(minutes=chat_minutes_ago)).isoformat()
            if chat_minutes_ago is not None
            else None,
            "last_daily": (self.now.date() - timedelta(days=1)).isoformat(),
            "stm_done": True,
            "routine_done": True,
        }

    def phase(self, chat_minutes_ago: float | None) -> str | None:
        with mock.patch.object(clock, "_now", return_value=self.now), mock.patch.object(
            clock, "_load", return_value=self.state(chat_minutes_ago)
        ), mock.patch.object(clock, "parse_schedule", return_value={"stm_minutes": 5}):
            return clock.due_phase("x")

    def test_daily_does_not_jump_a_live_conversation(self):
        self.assertIsNone(self.phase(chat_minutes_ago=2))

    def test_daily_runs_as_soon_as_the_window_closes(self):
        self.assertEqual(self.phase(chat_minutes_ago=9), "daily")

    def test_the_regular_catch_up_still_works_without_a_chat(self):
        self.assertEqual(self.phase(chat_minutes_ago=None), "daily")

    def test_a_hold_still_wins_over_everything(self):
        state = self.state(chat_minutes_ago=None)
        state["hold"] = True
        with mock.patch.object(clock, "_now", return_value=self.now), mock.patch.object(
            clock, "_load", return_value=state
        ), mock.patch.object(clock, "parse_schedule", return_value={"stm_minutes": 5}):
            self.assertIsNone(clock.due_phase("x"))


class MissedDailyTests(unittest.TestCase):
    def test_overnight_downtime_still_counts_as_missed(self):
        now = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
        self.assertTrue(clock.missed_daily(now, "2026-10-14"))

    def test_todays_daily_is_not_missed(self):
        now = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
        self.assertFalse(clock.missed_daily(now, "2026-10-16"))

    def test_after_23_it_is_due(self):
        now = datetime(2026, 10, 16, 23, 5, tzinfo=timezone.utc)
        self.assertTrue(clock.missed_daily(now, "2026-10-16") is False)


if __name__ == "__main__":
    unittest.main()
