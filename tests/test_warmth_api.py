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

    def test_the_command_window_knows_the_warmth_commands(self):
        """命令窗口是白名单分发器：这三条命令被删掉时，这条测试要响。

        真正的行为只能在浏览器里点，这里守的是"入口还在"。
        """
        response = self.client.get("/static/console.js")
        self.assertEqual(response.status_code, 200)
        body = response.text
        for needle in ('cmd === "warmth"', 'rest[0] === "watch"', 'cmd === "mark"', "/api/warmth/mark"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)

    def test_the_command_window_help_lists_them(self):
        body = self.client.get("/static/console.js").text
        for needle in ("warmth watch", "mark <结论> <说明>"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)

    def test_the_command_window_checks_the_verdict_word(self):
        """复核第二轮：`mark ok_silent ...` 原先被当成"没写结论"，记成 should_speak。

        真正的行为只能在 DOM 里跑（复核方用临时 DOM 验过）；这里钉住形状：
        认不出的词要**原样交给服务端**，且跑别的命令要停掉 watch。
        """
        body = self.client.get("/static/console.js").text
        self.assertIn("looksLikeVerdict", body)
        self.assertNotIn('verdicts.includes(first) ? first : "should_speak"', body)
        self.assertGreaterEqual(body.count("stopWatch();"), 5)

    def test_the_command_window_shows_fresh_and_total_backlog(self):
        body = self.client.get("/static/console.js").text
        self.assertIn("backlog_total", body)

    def test_the_panel_never_writes_user_text_with_innerhtml(self):
        """复核实测：台账里用户写的标注文字走 innerHTML 时 `<img onerror>` 会执行。

        这条守的是整页——面板里出现的文字有用户输入（标注、标签）和她的原话。
        """
        body = self.client.get("/static/warmth.js").text
        self.assertNotIn(".innerHTML", body)

    def test_the_panel_page_can_scroll_to_the_form(self):
        """复核：app.css 把 html/body 设成 overflow:hidden，1100×780 的窗口滚不到标注表单。"""
        page = self.client.get("/warmth").text
        self.assertIn("overflow: auto", page)


if __name__ == "__main__":
    unittest.main()
