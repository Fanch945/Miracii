"""Launch Miracii as a local GUI, web server, or LAN host for the phone client."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from . import APP_CHANNEL, APP_VERSION
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7788


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def wait_ready(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url + "/api/health", timeout=1)
            return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.15)
    return False


def start_server(host: str, port: int) -> threading.Thread:
    import uvicorn

    from .main import app

    thread = threading.Thread(
        target=lambda: uvicorn.run(app, host=host, port=port, log_level="warning"),
        name="miracii-http",
        daemon=True,
    )
    thread.start()
    return thread


def start_heartbeat() -> threading.Thread:
    from . import heartbeat

    def run() -> None:
        try:
            heartbeat.run_forever()
        except Exception as exc:
            print(f"heartbeat stopped: {exc}", flush=True)

    thread = threading.Thread(target=run, name="miracii-heartbeat", daemon=True)
    thread.start()
    return thread


def open_window(url: str) -> None:
    import webview

    webview.create_window(
        f"Miracii v{APP_VERSION}",
        url,
        width=1100,
        height=780,
        min_size=(360, 640),
        background_color="#161310",
    )
    webview.start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Miracii local companion")
    parser.add_argument("--web", action="store_true", help="only run the HTTP server (no window)")
    parser.add_argument("--lan", action="store_true", help="bind 0.0.0.0 so the Android client can connect")
    parser.add_argument("--no-heartbeat", action="store_true", help="do not start the consolidation loop")
    parser.add_argument("--host", default="", help="override bind host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    host = args.host or ("0.0.0.0" if args.lan else DEFAULT_HOST)
    port = args.port
    local = f"http://127.0.0.1:{port}"
    print(f"Miracii v{APP_VERSION} ({APP_CHANNEL})", flush=True)
    print(f"http {host}:{port}", flush=True)
    if args.lan or host == "0.0.0.0":
        print(f"phone: http://{lan_ip()}:{port}", flush=True)

    start_server(host, port)
    if not args.no_heartbeat:
        start_heartbeat()
        print("heartbeat on", flush=True)
    if not wait_ready(local):
        print("server did not become ready", file=sys.stderr)
        return 1

    if args.web or args.lan:
        print(f"open {local}", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    try:
        open_window(local)
    except ImportError:
        print("pywebview not installed; open this in a browser:", local)
        print("pip install pywebview", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
