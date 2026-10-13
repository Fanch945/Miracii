"""Star map: anchors, decay, recall, arbitration inputs — and the promise that memory files never change.

Every test runs against a throwaway persona tree in a temp directory; nothing here touches
the real `persona/` folder.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from app import persona, starmap

TODAY = date(2026, 10, 13)


class Tree(unittest.TestCase):
    """A minimal persona tree: impression blocks, semantic lines, one lore entry."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="starmap-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "memory" / "impression").mkdir(parents=True)
        (self.root / "memory" / "semantic").mkdir(parents=True)
        (self.root / "lore" / "entries").mkdir(parents=True)
        patcher = mock.patch.object(persona, "folder", lambda persona_id=None: self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def impression(self, name="2026-10-13.txt", mood="困、亲近、带点抱歉"):
        path = self.root / "memory" / "impression" / name
        path.write_text(
            f"# routine 2026-10-13 14:47\n那次：午饭后靠在一起。\n气氛：{mood}\nkeys: 计时, 午睡\n"
            "\n# routine 2026-10-13 20:10\n那次：晚上聊了人格维度。\n气氛：认真\nkeys: 维度\n",
            encoding="utf-8",
        )
        return path

    def semantic(self):
        path = self.root / "memory" / "semantic" / "people.txt"
        path.write_text("# 名字 | 关系 | 日期 | keys\n樊辰昊 | 研究生 | 2026-09-21 | 学校\n", encoding="utf-8")
        return path

    def lore_entry(self, name="labs.txt", form="digest"):
        path = self.root / "lore" / "entries" / name
        path.write_text(f"keys: 实验室\nform: {form}\n---\n她和他很难同实验室。\n", encoding="utf-8")
        return path

    def snapshot(self) -> dict[str, str]:
        return {
            p.relative_to(self.root).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in sorted(self.root.rglob("*"))
            if p.is_file() and p.name != starmap.INDEX_NAME
        }


class FingerprintTests(Tree):
    def test_reflow_does_not_change_identity(self):
        self.assertEqual(starmap.fingerprint("那次： 午饭\n气氛：困"), starmap.fingerprint("那次： 午饭  气氛：困"))

    def test_real_edit_changes_identity(self):
        self.assertNotEqual(starmap.fingerprint("那次：午饭"), starmap.fingerprint("那次：晚饭"))


class MoodWeightTests(unittest.TestCase):
    def test_missing_mood_falls_back(self):
        self.assertEqual(starmap.mood_weight(""), starmap.EW_FALLBACK)
        self.assertEqual(starmap.mood_weight(None), starmap.EW_FALLBACK)

    def test_six_bands(self):
        cases = {
            "困": 0.4,                      # one mild
            "困、累": 0.6,                  # two mild
            "心虚": 0.6,                    # one clear
            "担心、委屈": 0.8,              # two clear
            "有点崩溃": 0.8,                # one strong
            "崩溃、哭": 1.0,                # two strong
            "讨论了一下": 0.2,              # nothing recognised
        }
        for mood, expected in cases.items():
            with self.subTest(mood=mood):
                self.assertEqual(starmap.mood_weight(mood), expected)


class DecayTests(unittest.TestCase):
    def test_bands_and_half_lives(self):
        cases = {0.9: (0.005, 138.63), 0.6: (0.01, 69.31), 0.4: (0.02, 34.66), 0.3: (0.04, 17.33)}
        for ew, (lam, half) in cases.items():
            with self.subTest(ew=ew):
                self.assertEqual(starmap.decay_lambda(ew), lam)
                self.assertAlmostEqual(starmap.half_life_days(ew), half, places=1)

    def test_recall_bonus_is_logarithmic_and_capped(self):
        self.assertEqual(starmap.recall_bonus(0), 0.0)
        self.assertLess(starmap.recall_bonus(1), starmap.recall_bonus(10))
        self.assertEqual(starmap.recall_bonus(1000), 0.3)
        self.assertAlmostEqual(starmap.recall_bonus(41), 0.3, places=2)

    def test_a_fresh_high_emotion_star_is_full_brightness(self):
        star = {"ew": 0.8, "rc": 0, "created": TODAY.isoformat()}
        self.assertAlmostEqual(starmap.brightness(star, TODAY), 1.0, places=6)

    def test_high_emotion_decays_slower_than_trivia(self):
        old = (TODAY - timedelta(days=60)).isoformat()
        treasured = starmap.brightness({"ew": 0.8, "rc": 0, "created": old}, TODAY)
        trivia = starmap.brightness({"ew": 0.2, "rc": 0, "created": old}, TODAY)
        self.assertGreater(treasured, trivia)

    def test_recall_adds_brightness(self):
        old = (TODAY - timedelta(days=30)).isoformat()
        cold = starmap.brightness({"ew": 0.4, "rc": 0, "created": old}, TODAY)
        warm = starmap.brightness({"ew": 0.4, "rc": 20, "created": old}, TODAY)
        self.assertGreater(warm, cold)

    def test_rehearsal_does_not_reset_ageing(self):
        """Ageing counts from `created`: a line read today is still an old line."""
        old = (TODAY - timedelta(days=90)).isoformat()
        plain = {"ew": 0.4, "rc": 0, "created": old}
        rehearsed = {"ew": 0.4, "rc": 0, "created": old, "last": TODAY.isoformat()}
        self.assertAlmostEqual(starmap.decay_factor(plain, TODAY), starmap.decay_factor(rehearsed, TODAY))

    def test_novelty_penalty_is_bounded(self):
        self.assertEqual(starmap.novelty_factor(0), 1.0)
        self.assertLess(starmap.novelty_factor(20), 1.0)
        self.assertEqual(starmap.novelty_factor(10 ** 6), starmap.NOVELTY_FLOOR)

    def test_magnitude_runs_backwards(self):
        self.assertEqual(starmap.magnitude(1.0), 1.0)
        self.assertEqual(starmap.magnitude(0.0), 6.5)
        self.assertGreater(starmap.magnitude(0.2), starmap.magnitude(0.8))


class BlockTests(Tree):
    def test_walks_the_whole_tree_with_levels(self):
        self.impression()
        self.semantic()
        self.lore_entry()
        stars = starmap.blocks()
        kinds = sorted(star["kind"] for star in stars)
        self.assertEqual(kinds, ["impression", "impression", "lore", "semantic"])
        levels = {(star["kind"], star["block"]): star["level"] for star in stars}
        self.assertEqual(levels[("impression", 0)], 2)
        self.assertEqual(levels[("semantic", 0)], 1)
        self.assertEqual(levels[("lore", 0)], 1)

    def test_story_entries_sit_one_level_up(self):
        self.lore_entry(name="first-day.txt", form="story")
        self.assertEqual(starmap.blocks()[0]["level"], starmap.STORY_LEVEL)

    def test_mood_line_becomes_emotional_weight(self):
        self.impression(mood="崩溃、哭")
        stars = starmap.blocks()
        # block 0: 崩溃、哭 -> two strong words -> 1.0 ; block 1: 认真 -> one mild word -> 0.4
        self.assertEqual([star["ew"] for star in stars], [1.0, 0.4])


class SyncTests(Tree):
    def test_first_sync_adds_then_identical_retry_defers(self):
        self.impression()
        first = starmap.sync(today=TODAY)
        self.assertEqual(first, {"added": 2, "retired": 0, "deferred": 0})
        rows = starmap.index_path().read_text(encoding="utf-8").count("\n")
        second = starmap.sync(today=TODAY)
        self.assertEqual(second, {"added": 0, "retired": 0, "deferred": 2})
        self.assertEqual(starmap.index_path().read_text(encoding="utf-8").count("\n"), rows)

    def test_edited_block_retires_the_old_star_and_enters_again(self):
        path = self.impression()
        starmap.sync(today=TODAY)
        path.write_text(path.read_text(encoding="utf-8").replace("午饭后靠在一起", "午饭后各自忙"), encoding="utf-8")
        result = starmap.sync(today=TODAY)
        self.assertEqual(result["retired"], 1)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["deferred"], 1)
        active = {(star["file"], star["block"]) for star in starmap.project(today=TODAY)}
        self.assertEqual(len(active), 2)

    def test_deleted_file_retires_its_stars(self):
        path = self.impression()
        starmap.sync(today=TODAY)
        path.unlink()
        self.assertEqual(starmap.sync(today=TODAY)["retired"], 2)
        self.assertEqual(starmap.project(today=TODAY), [])

    def test_sync_never_touches_a_memory_file(self):
        """The hard constraint: the sidecar grows, the memory tree stays byte-identical."""
        self.impression()
        self.semantic()
        self.lore_entry()
        before = self.snapshot()
        starmap.sync(today=TODAY)
        starmap.sync(today=TODAY)
        starmap.report(today=TODAY)
        self.assertEqual(before, self.snapshot())

    def test_index_is_readable_json_lines(self):
        self.impression()
        starmap.sync(today=TODAY)
        rows = [json.loads(line) for line in starmap.index_path().read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(row["state"] == "active" for row in rows))
        self.assertTrue(all(len(row["fp"]) == 12 for row in rows))


class RecallTests(Tree):
    def test_bump_counts_hits_and_moves_last_access(self):
        self.impression()
        starmap.sync(today=TODAY)
        file = starmap.blocks()[0]["file"]
        self.assertTrue(starmap.bump(None, file, 0, today=TODAY + timedelta(days=5)))
        star = starmap.load()[(file, 0)]
        self.assertEqual(star["rc"], 1)
        self.assertEqual(star["last"], (TODAY + timedelta(days=5)).isoformat())

    def test_bump_ignores_unknown_or_retired_stars(self):
        self.assertFalse(starmap.bump(None, "memory/impression/nope.txt", 0, today=TODAY))

    def test_report_lists_bright_and_dim(self):
        self.impression()
        starmap.sync(today=TODAY)
        text = starmap.report(today=TODAY)
        self.assertIn("2 个活跃星点", text)
        self.assertIn("最亮：", text)
        self.assertIn("最暗：", text)

    def test_report_on_empty_tree_is_honest(self):
        self.assertIn("还没有星点", starmap.report(today=TODAY))


class ReviewFixTests(Tree):
    """Cases raised by the reviewer: dates must not be invented, history must not be inherited."""

    def test_authored_canon_has_no_date_and_no_brightness(self):
        self.lore_entry()
        star = starmap.blocks()[0]
        self.assertIsNone(star["created"])
        self.assertEqual(starmap.decay_factor(star, TODAY), 1.0)
        self.assertIsNone(starmap.brightness(star, TODAY))

    def test_mtime_never_becomes_created(self):
        import os

        path = self.lore_entry()
        os.utime(path, (0, 0))  # touch it: a fresh mtime must not make canon look fresh
        self.assertIsNone(starmap.blocks()[0]["created"])

    def test_report_puts_undated_stars_on_their_own_shelf(self):
        self.impression()
        self.lore_entry()
        starmap.sync(today=TODAY)
        text = starmap.report(today=TODAY)
        self.assertIn("无日期（作者设定", text)
        self.assertIn("lore/entries/labs.txt#0", text)
        self.assertNotIn("mag None", text)

    def test_an_edited_block_does_not_inherit_history(self):
        path = self.impression()
        starmap.sync(today=TODAY)
        file = starmap.blocks()[0]["file"]
        starmap.bump(None, file, 0, today=TODAY)
        self.assertEqual(starmap.load()[(file, 0)]["rc"], 1)
        edited = path.read_text(encoding="utf-8").replace("午饭后靠在一起", "午饭后各自忙")
        path.write_text(edited, encoding="utf-8")
        starmap.sync(today=TODAY)
        self.assertEqual(starmap.load()[(file, 0)]["rc"], 0)  # new content, new star

    def test_src_identifies_the_block_not_just_the_file(self):
        self.impression()
        self.semantic()
        srcs = [star["src"] for star in starmap.blocks()]
        self.assertEqual(len(srcs), len(set(srcs)))
        self.assertTrue(all("#" in src for src in srcs))


if __name__ == "__main__":
    unittest.main()
