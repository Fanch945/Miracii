"""积温面板的网页接口：只读状态 + 一个写入口（标注）。

`TestClient` 故意**不用上下文管理器**——那样不会触发 lifespan，
也就不会在测试里跑 `starmap.sync` 去动真实人格的文件。
"""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app import main, warmth


class WarmthApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_the_page_is_served(self):
        response = self.client.get("/warmth")
        self.assertEqual(response.status_code, 200)
        self.assertIn("积温", response.text)
        self.assertIn("/static/warmth.js", response.text)

    def test_the_view_endpoint_reports_every_axis(self):
        response = self.client.get("/api/warmth")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            set(data["axes"]), {"connection", "pride", "valence", "arousal", "immersion", "hesitation"}
        )
        self.assertIn("gates", data)
        self.assertIn("tick_minutes", data)
        self.assertIn("recent", data)

    def test_the_view_endpoint_does_not_write_anything(self):
        before = warmth.read_log("test-persona-not-real")
        self.client.get("/api/warmth", params={"persona": "test-persona-not-real"})
        self.assertEqual(before, warmth.read_log("test-persona-not-real"))

    def test_marking_posts_a_mark_through_the_same_function_the_cli_uses(self):
        with mock.patch.object(main.warmth, "mark", return_value={"verdict": "should_speak"}) as mark:
            response = self.client.post(
                "/api/warmth/mark",
                json={
                    "text": "他刚说很难过的事",
                    "verdict": "should_speak",
                    "blame": ["pride", "hesitation"],
                    "expected": "contact",
                    "tags": ["他情绪低落"],
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["verdict"], "should_speak")
        kwargs = mark.call_args.kwargs
        self.assertEqual(kwargs["verdict"], "should_speak")
        self.assertEqual(kwargs["blame"], ["pride", "hesitation"])
        self.assertEqual(kwargs["expected"], "contact")
        self.assertEqual(kwargs["tags"], ["他情绪低落"])

    def test_a_failure_inside_mark_becomes_a_400_not_a_500(self):
        with mock.patch.object(main.warmth, "mark", side_effect=RuntimeError("磁盘满了")):
            response = self.client.post("/api/warmth/mark", json={"text": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("磁盘满了", response.json()["detail"])

    def test_the_payload_shape_is_validated(self):
        response = self.client.post("/api/warmth/mark", json={"text": "x" * 5000})
        self.assertEqual(response.status_code, 422)  # 超长说明被 pydantic 拦住

    def test_a_bare_mark_gets_sensible_defaults(self):
        with mock.patch.object(main.warmth, "mark", return_value={}) as mark:
            self.client.post("/api/warmth/mark", json={})
        kwargs = mark.call_args.kwargs
        self.assertEqual(kwargs["verdict"], "should_speak")
        self.assertEqual(kwargs["blame"], [])
        self.assertEqual(kwargs["tags"], [])


if __name__ == "__main__":
    unittest.main()
