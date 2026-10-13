"""Retrieval ranking: keyword relevance first, then ageing and familiarity from the star map.

Every case runs against a throwaway persona tree; the real `persona/` folder is untouched.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from app import persona, recall, starmap

TODAY = date.today()


def _days_ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


class Tree(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="recall-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for folder in ("impression", "episodic", "semantic"):
            (self.root / "memory" / folder).mkdir(parents=True)
        patcher = mock.patch.object(persona, "folder", lambda persona_id=None: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def semantic(self, rows: list[str], name: str = "people.txt"):
        path = self.root / "memory" / "semantic" / name
        path.write_text("# 名字 | 关系 | 日期 | keys\n" + "\n".join(rows) + "\n", encoding="utf-8")
        return path

    def sitting(self, folder: str, day: str, mood: str, keys: str, body: str = "那次：坐下聊了一会儿。"):
        path = self.root / "memory" / folder / f"{day}.txt"
        text = f"# routine {day} 20:00\n{body}\n气氛：{mood}\nkeys: {keys}\n"
        if path.exists():
            text = path.read_text(encoding="utf-8") + "\n" + text
        path.write_text(text, encoding="utf-8")
        return path

    def star_for(self, text: str) -> dict | None:
        return starmap.lookup().get(starmap.fingerprint(text))


class FallbackTests(Tree):
    """No sidecar at all: the module must behave exactly like the old keyword scorer."""

    def test_plain_keyword_order_when_no_star_map_exists(self):
        self.semantic(["甲 | 先说的一行 | 2026-09-01 | 学校", "乙 | 后说的一行 | 2026-09-01 | 学校"])
        self.assertFalse(starmap.index_path().exists())
        picked = recall.select_semantic(None, "聊到学校的事")
        self.assertEqual(picked, ["乙 | 后说的一行 | 2026-09-01 | 学校", "甲 | 先说的一行 | 2026-09-01 | 学校"])

    def test_format_works_without_a_star_map(self):
        self.semantic(["樊辰昊 | 在读研究生 | 2026-09-21 | 学校"])
        text = recall.format_for_prompt(None, "他学校那边怎么样")
        self.assertIn("【已采纳的事实】", text)
        self.assertIn("在读研究生", text)


class RankingTests(Tree):
    def test_familiar_lines_yield_to_fresh_ones(self):
        self.semantic(["甲 | 老被提起的一行 | 2026-09-01 | 学校", "乙 | 很少提起的一行 | 2026-09-01 | 学校"])
        starmap.sync()
        for _ in range(20):
            starmap.bump_many(None, [starmap.fingerprint("甲 | 老被提起的一行 | 2026-09-01 | 学校")])
        picked = recall.select_semantic(None, "聊到学校的事")
        self.assertEqual(picked[0], "乙 | 很少提起的一行 | 2026-09-01 | 学校")

    def test_older_memories_rank_lower(self):
        self.semantic([f"甲 | 很久以前的一行 | {_days_ago(300)} | 学校", f"乙 | 最近的一行 | {_days_ago(1)} | 学校"])
        starmap.sync()
        picked = recall.select_semantic(None, "聊到学校的事")
        self.assertEqual(picked[0], f"乙 | 最近的一行 | {_days_ago(1)} | 学校")

    def test_a_treasured_old_memory_outlasts_trivia(self):
        """Same relevance, different emotional weight: the strong one ages more slowly."""
        self.sitting("impression", _days_ago(200), "崩溃、哭", "实验室")
        self.sitting("impression", _days_ago(60), "困", "实验室")
        starmap.sync()
        picked = recall.select_sittings(None, "实验室那边")
        self.assertEqual(len(picked), 2)
        self.assertIn("崩溃", picked[0])

    def test_ranking_does_not_use_brightness(self):
        """A rehearsed line is bright, yet ranking must still push it down."""
        self.semantic(["甲 | 常被想起 | 2026-09-01 | 学校", "乙 | 偶尔想起 | 2026-09-01 | 学校"])
        starmap.sync()
        line = "甲 | 常被想起 | 2026-09-01 | 学校"
        for _ in range(20):
            starmap.bump_many(None, [starmap.fingerprint(line)])
        star = self.star_for(line)
        self.assertGreater(starmap.brightness(star), starmap.brightness(self.star_for("乙 | 偶尔想起 | 2026-09-01 | 学校")))
        self.assertEqual(recall.select_semantic(None, "聊到学校的事")[0], "乙 | 偶尔想起 | 2026-09-01 | 学校")


class ArbitrationTests(Tree):
    def test_the_archived_copy_does_not_come_twice(self):
        day = _days_ago(3)
        block = f"# routine {day} 20:00\n那次：午饭后靠在一起。\n气氛：困\nkeys: 午睡"
        self.sitting("impression", day, "困", "午睡", body="那次：午饭后靠在一起。")
        self.sitting("episodic", day, "困", "午睡", body="那次：午饭后靠在一起。")
        starmap.sync()
        text = recall.format_for_prompt(None, "午睡那事儿")
        self.assertEqual(text.count("午饭后靠在一起"), 1)  # injected once, not twice
        # the live copy is the one that gets credited; the archived copy stays cold
        live = self.star_for(block)
        self.assertEqual(live["file"], f"memory/impression/{day}.txt")
        self.assertEqual(live["rc"], 1)
        archived = starmap.load()[(f"memory/episodic/{day}.txt", 0)]
        self.assertEqual(archived["rc"], 0)

    def test_a_fact_inside_an_injected_scene_is_not_repeated(self):
        line = "樊辰昊 | 在读研究生 | 2026-09-21 | 学校"
        self.semantic([line])
        self.sitting("impression", _days_ago(2), "认真", "学校", body=f"那次：聊到学校。他自述：{line}")
        starmap.sync()
        text = recall.format_for_prompt(None, "学校那边怎么样")
        self.assertIn("【那次印象】", text)
        self.assertEqual(text.count("在读研究生"), 1)

    def test_intent_without_keys_still_falls_back_to_the_newest(self):
        self.sitting("impression", _days_ago(9), "困", "无关")
        self.sitting("impression", _days_ago(1), "安静", "别的")
        starmap.sync()
        picked = recall.select_sittings(None, "你还记得那次吗")
        self.assertEqual(len(picked), 1)
        self.assertIn("别的", picked[0])


class BookkeepingTests(Tree):
    def test_injections_are_counted_once_per_turn(self):
        self.semantic(["樊辰昊 | 在读研究生 | 2026-09-21 | 学校"])
        starmap.sync()
        line = "樊辰昊 | 在读研究生 | 2026-09-21 | 学校"
        self.assertEqual(self.star_for(line)["rc"], 0)
        recall.format_for_prompt(None, "学校那边怎么样")
        self.assertEqual(self.star_for(line)["rc"], 1)
        recall.format_for_prompt(None, "学校那边怎么样")
        self.assertEqual(self.star_for(line)["rc"], 2)

    def test_a_broken_sidecar_never_breaks_a_reply(self):
        self.semantic(["樊辰昊 | 在读研究生 | 2026-09-21 | 学校"])
        starmap.sync()
        with mock.patch.object(starmap, "bump_many", side_effect=OSError("disk full")):
            text = recall.format_for_prompt(None, "学校那边怎么样")
        self.assertIn("在读研究生", text)


if __name__ == "__main__":
    unittest.main()
