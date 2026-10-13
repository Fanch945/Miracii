"""Desktop ping when she speaks up unprompted: a Windows toast, plus an optional panel.

Only fires while the chat window is not in front; a ping must never take the
heartbeat down nor keep the tick waiting. The floating panel is **off by default**
(`POPUP_ENABLED`): it lands in the same bottom-right corner as the toast and the two
cover each other, and the toast alone is already unmistakable. ctypes, tkinter and
Windows PowerShell 5.1 are the only tools used, so requirements.txt does not change.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

TITLE = "Miracii"
TOAST_CHARS = 140
BODY_CHARS = 180
POPUP_MS = 7000
POPUP_GAP = 12
POPUP_READY_S = 2.0
PS_TIMEOUT_S = 8
POPUP_MIN_TOP = 80
POPUP_STACK_MAX = 4
# Off by default. The toast already occupies the same corner and covers the panel, so the
# panel only adds noise. Everything below it (click-to-focus included) stays in place and
# is covered by tests: flip this to True to get the panel back.
POPUP_ENABLED = False
AUMID = "Miracii"
# Used while the Miracii AppUserModelId is not registered; the toast then reads Windows PowerShell.
AUMID_FALLBACK = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_INK = "#161310"
_PANEL = "#1f1a16"
_LINE = "#3a332c"
_GOLD = "#c9a36a"
_PAPER = "#efe4d4"
_MUTE = "#9a8d7d"

_FONT = "Microsoft YaHei UI"

_PS_TOAST = """$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('{xml}')
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{aumid}').Show($toast)
"""

_stack: list = []
_stack_lock = threading.Lock()


def _foreground() -> tuple[int, str]:
    """`(pid, title)` of the foreground window; `(0, "")` when there is none."""
    if os.name != "nt":
        return 0, ""
    user32 = ctypes.windll.user32
    handle = user32.GetForegroundWindow()
    if not handle:
        return 0, ""
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    length = user32.GetWindowTextLengthW(handle)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, length + 1)
    return int(pid.value), buffer.value


def window_focused() -> bool:
    """True when the chat window is in front — the desktop window, or a browser tab on it."""
    pid, title = _foreground()
    if pid and pid == os.getpid():
        return True
    return title.strip().lower().startswith(TITLE.lower())


def focus_app() -> bool:
    """Bring the chat window forward. The desktop window, or the browser tab on the page."""
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(handle, _param):
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        if buffer.value.strip().lower().startswith(TITLE.lower()):
            found.append(int(handle))
            return False
        return True

    user32.EnumWindows(visit, 0)
    if not found:
        return False
    user32.ShowWindow(found[0], 9)
    return bool(user32.SetForegroundWindow(found[0]))


def _enabled(persona_id: str) -> bool:
    from . import supervisor

    return bool(supervisor.parse_schedule(persona_id).get("notify", True))


def _squash(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _toast_xml(title: str, body: str) -> str:
    return (
        '<toast duration="short"><visual><binding template="ToastGeneric">'
        f"<text>{_escape(title)}</text><text>{_escape(body)}</text>"
        "</binding></visual></toast>"
    )


def _powershell() -> str | None:
    found = shutil.which("powershell.exe") or shutil.which("powershell")
    if found:
        return found
    fallback = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    return str(fallback) if fallback.is_file() else None


def _register_aumid() -> bool:
    """True when the toast can read "Miracii" instead of "Windows PowerShell"."""
    import winreg

    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\AppUserModelId\{AUMID}",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, AUMID)
    except OSError:
        return False
    return True


def _toast(title: str, body: str) -> bool:
    """One PowerShell call, one AppUserModelId: nothing here is worth a second wait."""
    if os.name != "nt":
        return False
    shell = _powershell()
    if not shell:
        return False
    aumid = AUMID if _register_aumid() else AUMID_FALLBACK
    script = _PS_TOAST.format(xml=_toast_xml(title, body), aumid=aumid)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=PS_TIMEOUT_S,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _popup_window(title: str, body: str, ready: threading.Event) -> None:
    try:
        import tkinter as tk
    except ImportError:
        return
    try:
        root = tk.Tk()
    except tk.TclError:
        return
    try:
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        width, height = 360, 110
        with _stack_lock:
            if len(_stack) >= POPUP_STACK_MAX:
                _stack.pop(0)
            _stack.append(root)
            depth = len(_stack) - 1
        left = root.winfo_screenwidth() - width - 24
        top = max(
            POPUP_MIN_TOP,
            root.winfo_screenheight() - height - 72 - depth * (height + POPUP_GAP),
        )
        root.geometry(f"{width}x{height}+{left}+{top}")

        frame = tk.Frame(root, background=_PANEL, highlightbackground=_LINE, highlightthickness=1)
        frame.pack(fill="both", expand=True)
        name = tk.Label(
            frame,
            text=title,
            background=_PANEL,
            foreground=_GOLD,
            font=(_FONT, 10, "bold"),
            anchor="w",
        )
        name.pack(fill="x", padx=14, pady=(11, 0))
        text = tk.Label(
            frame,
            text=body,
            background=_PANEL,
            foreground=_PAPER,
            font=(_FONT, 10),
            anchor="w",
            justify="left",
            wraplength=width - 32,
        )
        text.pack(fill="x", padx=14, pady=(4, 0))
        hint = tk.Label(
            frame,
            text="点一下打开 Miracii",
            background=_PANEL,
            foreground=_MUTE,
            font=(_FONT, 8),
            anchor="w",
        )
        hint.pack(fill="x", padx=14, pady=(6, 10))

        def close(_event=None) -> None:
            with _stack_lock:
                if root in _stack:
                    _stack.remove(root)
            try:
                root.destroy()
            except tk.TclError:
                pass

        def open_app(_event=None) -> None:
            focus_app()
            close()

        for widget in (root, frame, name, text, hint):
            widget.bind("<Button-1>", open_app)

        root.deiconify()
        root.update_idletasks()
        root.update()
        ready.set()
        root.after(POPUP_MS, close)
        root.mainloop()
    except tk.TclError:
        with _stack_lock:
            if root in _stack:
                _stack.remove(root)
        return


def _popup(title: str, body: str) -> bool:
    """True only once the panel is actually on screen."""
    if os.name != "nt":
        return False
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    ready = threading.Event()
    threading.Thread(
        target=_popup_window, args=(title, body, ready), name="miracii-popup", daemon=True
    ).start()
    return ready.wait(POPUP_READY_S)


def proactive(persona_id: str, speaker: str, text: str, *, force: bool = False) -> dict:
    """Ping the desktop about one unprompted line. The result is what the heartbeat logs."""
    if not force:
        if window_focused():
            return {"fired": False, "reason": "focused"}
        if not _enabled(persona_id):
            return {"fired": False, "reason": "off"}
    name = str(speaker or TITLE)
    result: dict = {"fired": False}
    try:
        result["toast"] = _toast(name, _squash(text, TOAST_CHARS))
    except Exception as exc:  # a reminder must never reach the tick as an exception
        result["toast"] = False
        result["toast_error"] = f"{type(exc).__name__}: {exc}"
    if POPUP_ENABLED:
        try:
            result["popup"] = _popup(name, _squash(text, BODY_CHARS))
        except Exception as exc:
            result["popup"] = False
            result["popup_error"] = f"{type(exc).__name__}: {exc}"
    result["fired"] = bool(result["toast"] or result.get("popup"))
    return result


if __name__ == "__main__":
    import argparse

    from . import persona

    parser = argparse.ArgumentParser(description="Miracii desktop ping (test)")
    parser.add_argument("text", nargs="?", default="测试：她主动开口时的通知")
    parser.add_argument("--name", default=TITLE)
    parser.add_argument("--force", action="store_true", help="ignore the foreground window and the switch")
    options = parser.parse_args()
    result = proactive(persona.active_id(), options.name, options.text, force=options.force)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if result.get("popup"):
        time.sleep(POPUP_MS / 1000 + 0.5)
