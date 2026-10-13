"""Desktop ping: the decision rules, the toast payload, the wiring into deliver_speak.

Nothing pops up on a normal run. A real toast/popup only happens with
`MIRACII_NOTIFY_TEST=1 python -m unittest tests.test_notify`.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from app import notify, supervisor


class SquashTests(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(notify._squash("  她\n\n说 了  一句 ", 40), "她 说 了 一句")

    def test_truncates_to_the_limit(self):
        out = notify._squash("字" * 30, 10)
        self.assertEqual(len(out), 10)
        self.assertTrue(out.endswith("…"))

    def test_empty_stays_empty(self):
        self.assertEqual(notify._squash(None, 10), "")


class ToastPayloadTests(unittest.TestCase):
    def test_escapes_markup(self):
        self.assertEqual(notify._escape("a<b>&\"c'"), "a&lt;b&gt;&amp;&quot;c&apos;")

    def test_xml_carries_both_lines(self):
        xml = notify._toast_xml("Miracii", "5 < 6 & \"7\"")
        self.assertIn("<text>Miracii</text>", xml)
        self.assertIn("5 &lt; 6 &amp; &quot;7&quot;", xml)
        self.assertNotIn("5 < 6", xml)

    def test_script_keeps_the_xml_inside_one_powershell_literal(self):
        script = notify._PS_TOAST.format(xml=notify._toast_xml("A", "it's"), aumid="Miracii")
        self.assertIn("it&apos;s", script)
        self.assertNotIn("it's", script)
        self.assertIn("CreateToastNotifier('Miracii')", script)

    def test_one_call_one_app_id(self):
        """The reviewer's point: no second PowerShell wait when the first fails."""
        with mock.patch.object(notify, "_register_aumid", return_value=True), mock.patch.object(
            notify, "_powershell", return_value="powershell.exe"
        ), mock.patch.object(notify.subprocess, "run") as run:
            run.side_effect = OSError("boom")
            self.assertFalse(notify._toast("Miracii", "在吗"))
        self.assertEqual(run.call_count, 1)


class FocusTests(unittest.TestCase):
    def test_our_own_window_is_focused(self):
        with mock.patch.object(notify, "_foreground", return_value=(os.getpid(), "anything")):
            self.assertTrue(notify.window_focused())

    def test_a_browser_tab_on_the_page_is_focused(self):
        with mock.patch.object(notify, "_foreground", return_value=(4321, "Miracii - Chrome")):
            self.assertTrue(notify.window_focused())

    def test_some_other_window_is_not_focused(self):
        with mock.patch.object(notify, "_foreground", return_value=(4321, "Visual Studio Code")):
            self.assertFalse(notify.window_focused())


class ProactiveTests(unittest.TestCase):
    def setUp(self):
        self.calls: list[str] = []
        for name in ("_toast", "_popup"):
            patcher = mock.patch.object(
                notify, name, side_effect=lambda *a, _n=name: self.calls.append(_n) or True
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_silent_while_the_window_is_focused(self):
        with mock.patch.object(notify, "window_focused", return_value=True):
            result = notify.proactive("holmes-test", "弥", "在吗")
        self.assertEqual(result, {"fired": False, "reason": "focused"})
        self.assertEqual(self.calls, [])

    def test_silent_when_the_switch_is_off(self):
        with mock.patch.object(notify, "window_focused", return_value=False), mock.patch.object(
            notify, "_enabled", return_value=False
        ):
            result = notify.proactive("holmes-test", "弥", "在吗")
        self.assertEqual(result, {"fired": False, "reason": "off"})
        self.assertEqual(self.calls, [])

    def test_fires_the_toast_in_the_background(self):
        with mock.patch.object(notify, "window_focused", return_value=False), mock.patch.object(
            notify, "_enabled", return_value=True
        ):
            result = notify.proactive("holmes-test", "弥", "在吗")
        self.assertTrue(result["fired"])
        self.assertEqual(self.calls, ["_toast"])
        self.assertNotIn("popup", result)

    def test_the_panel_stays_off_by_default(self):
        """The toast already covers the same corner; the panel only adds noise."""
        self.assertFalse(notify.POPUP_ENABLED)

    def test_the_panel_still_works_when_switched_on(self):
        with mock.patch.object(notify, "POPUP_ENABLED", True), mock.patch.object(
            notify, "window_focused", return_value=False
        ), mock.patch.object(notify, "_enabled", return_value=True):
            result = notify.proactive("holmes-test", "弥", "在吗")
        self.assertEqual(self.calls, ["_toast", "_popup"])
        self.assertTrue(result["popup"])

    def test_force_ignores_focus_and_switch(self):
        with mock.patch.object(notify, "window_focused", return_value=True), mock.patch.object(
            notify, "_enabled", return_value=False
        ):
            result = notify.proactive("holmes-test", "弥", "在吗", force=True)
        self.assertTrue(result["fired"])

    def test_a_broken_layer_is_reported_not_raised(self):
        with mock.patch.object(notify, "POPUP_ENABLED", True), mock.patch.object(
            notify, "window_focused", return_value=False
        ), mock.patch.object(notify, "_enabled", return_value=True), mock.patch.object(
            notify, "_toast", side_effect=RuntimeError("no winrt")
        ):
            result = notify.proactive("holmes-test", "弥", "在吗")
        self.assertFalse(result["toast"])
        self.assertIn("no winrt", result["toast_error"])
        self.assertTrue(result["fired"])  # the panel still showed up


class ScheduleSwitchTests(unittest.TestCase):
    def test_on_by_default(self):
        with mock.patch.object(supervisor, "_read", return_value="quiet: 00:30-08:30\n"):
            self.assertTrue(supervisor.parse_schedule("holmes-test")["notify"])

    def test_explicit_off(self):
        with mock.patch.object(supervisor, "_read", return_value="notify: off\n"):
            self.assertFalse(supervisor.parse_schedule("holmes-test")["notify"])

    def test_spelled_out_values(self):
        for raw in ("no", "false", "0", "OFF"):
            with self.subTest(raw=raw), mock.patch.object(
                supervisor, "_read", return_value=f"notify: {raw}\n"
            ):
                self.assertFalse(supervisor.parse_schedule("holmes-test")["notify"])


class DeliverSpeakTests(unittest.TestCase):
    """deliver_speak must ping after saving, and a broken ping must not escape with the tick.

    Every side effect is stubbed, so nothing is written to the repository or to data/.
    """

    def _run(self, **ping):
        from app import heartbeat

        with mock.patch.object(heartbeat, "_may_speak", return_value=None), mock.patch.object(
            heartbeat.persona, "system_prompt", return_value="system"
        ), mock.patch.object(heartbeat.persona, "display_name", return_value="弥"), mock.patch.object(
            heartbeat.db, "add_message", return_value={"created_at": "2026-10-13T00:00:00+08:00"}
        ), mock.patch.object(heartbeat.files, "append_turn"), mock.patch.object(
            heartbeat.clock, "note_speak"
        ), mock.patch.object(heartbeat, "_log"), mock.patch(
            "app.llm.complete_reply", return_value="我在。"
        ), mock.patch.object(heartbeat.notify, "proactive", **ping) as patched:
            heartbeat.deliver_speak(
                "holmes-test", {"speak": True, "topic": "由头"}, {"persona_id": "holmes-test"}, force=True
            )
        return patched

    def test_pings_with_the_line_that_was_saved(self):
        patched = self._run(return_value={"fired": True, "toast": True, "popup": True})
        self.assertEqual(patched.call_count, 1)
        self.assertEqual(patched.call_args.args, ("holmes-test", "弥", "我在。"))

    def test_a_failing_ping_does_not_escape(self):
        patched = self._run(side_effect=OSError("no powershell"))
        self.assertEqual(patched.call_count, 1)


@unittest.skipUnless(os.environ.get("MIRACII_NOTIFY_TEST"), "set MIRACII_NOTIFY_TEST=1 to show a real ping")
class LivePingTests(unittest.TestCase):
    def test_real_toast(self):
        result = notify.proactive("holmes-test", "Miracii", "真实通知测试", force=True)
        self.assertTrue(result["fired"], result)


if __name__ == "__main__":
    unittest.main()
