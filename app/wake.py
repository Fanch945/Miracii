"""UDP interrupt: chat pokes heartbeat so it need not poll."""

from __future__ import annotations

import socket

WAKE_HOST = "127.0.0.1"
WAKE_PORT = 7791


def ping() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(b"wake", (WAKE_HOST, WAKE_PORT))
    except OSError:
        pass
    finally:
        sock.close()


def bind() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((WAKE_HOST, WAKE_PORT))
    return sock


def wait(sock: socket.socket, seconds: float) -> bool:
    """True if a chat interrupt arrived. False if the deadline elapsed."""
    sock.settimeout(max(0.05, seconds))
    try:
        sock.recvfrom(64)
        sock.settimeout(0)
        while True:
            try:
                sock.recvfrom(64)
            except OSError:
                break
        return True
    except (TimeoutError, socket.timeout):
        return False
    except OSError:
        return False
