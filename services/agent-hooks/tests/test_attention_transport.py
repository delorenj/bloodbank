from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

AGENT_HOOKS_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_HOOKS_DIR))

from clients.base import MAX_STDIN_BYTES, read_stdin_text
from core import nats_publish


def _fake_nats(mode: str) -> tuple[str, int, threading.Thread]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            with conn:
                conn.sendall(b'INFO {"server_id":"attention-transport"}\r\n')
                received = bytearray()
                conn.settimeout(2)
                while b"PING\r\n" not in received:
                    chunk = conn.recv(8192)
                    if not chunk:
                        return
                    received.extend(chunk)
                if mode == "pong":
                    conn.sendall(b"PONG\r\n")
                elif mode == "stall":
                    time.sleep(1.0)
                elif mode != "eof":
                    raise AssertionError(f"unknown mode {mode}")
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return host, port, thread


class AttentionTransportTests(unittest.TestCase):
    def test_deckard_nats_is_a_compatibility_fallback_with_explicit_overrides(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"DECKARD_NATS": "nats://compat.test:4333", "BLOODBANK_NATS_TIMEOUT": "0.5"},
            clear=True,
        ):
            self.assertEqual(nats_publish._config(), ("compat.test", 4333, 0.5))
        with mock.patch.dict(
            os.environ,
            {
                "DECKARD_NATS": "invalid:compat:that-must-be-ignored",
                "BLOODBANK_NATS_HOST": "explicit.test",
                "BLOODBANK_NATS_PORT": "4444",
            },
            clear=True,
        ):
            self.assertEqual(nats_publish._config(), ("explicit.test", 4444, 3.0))
        with mock.patch.dict(os.environ, {"DECKARD_NATS": "[::1]:4555"}, clear=True):
            self.assertEqual(nats_publish._config(), ("::1", 4555, 3.0))

    def test_eof_without_pong_is_a_publish_failure(self) -> None:
        host, port, server = _fake_nats("eof")
        with mock.patch.dict(
            os.environ,
            {"BLOODBANK_NATS_HOST": host, "BLOODBANK_NATS_PORT": str(port)},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "EOF before PONG"):
                nats_publish.publish("deckard.evt.attention", b"{}", timeout=0.5)
        server.join(timeout=2)

    def test_stalling_server_obeys_one_bounded_deadline(self) -> None:
        host, port, server = _fake_nats("stall")
        with mock.patch.dict(
            os.environ,
            {"BLOODBANK_NATS_HOST": host, "BLOODBANK_NATS_PORT": str(port)},
            clear=True,
        ):
            began = time.monotonic()
            with self.assertRaises(TimeoutError):
                nats_publish.publish(
                    "deckard.evt.attention", b"{}", timeout=0.08
                )
            self.assertLess(time.monotonic() - began, 0.5)
        server.join(timeout=2)

    def test_dns_resolution_is_inside_the_same_deadline(self) -> None:
        real_resolve = socket.getaddrinfo

        def slow_resolve(*args, **kwargs):
            time.sleep(0.3)
            return real_resolve(*args, **kwargs)

        with mock.patch("core.nats_publish.socket.getaddrinfo", side_effect=slow_resolve):
            began = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "during DNS"):
                nats_publish.publish(
                    "deckard.evt.attention", b"{}", timeout=0.05
                )
            self.assertLess(time.monotonic() - began, 0.2)

    def test_stdin_read_is_byte_bounded(self) -> None:
        raw = read_stdin_text(io.BytesIO(b"x" * (MAX_STDIN_BYTES * 3)))
        self.assertEqual(len(raw.encode("utf-8")), MAX_STDIN_BYTES)

    @unittest.skipUnless(sys.platform.startswith("linux"), "hook timing contract is Linux")
    def test_native_subprocess_fails_open_with_unclosed_stdin_and_stalled_nats(self) -> None:
        host, port, server = _fake_nats("stall")
        environment = os.environ.copy()
        environment.update(
            {
                "BLOODBANK_ENABLED": "true",
                "BLOODBANK_NATS_HOST": host,
                "BLOODBANK_NATS_PORT": str(port),
                "ZELLIJ_PANE_ID": "41",
                "ZELLIJ_SESSION_NAME": "Workspace",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        process = subprocess.Popen(
            [
                sys.executable,
                str(AGENT_HOOKS_DIR / "publish.py"),
                "--client",
                "claude",
                "--hook",
                "Notification",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        began = time.monotonic()
        try:
            returncode = process.wait(timeout=2.8)
        finally:
            if process.stdin:
                process.stdin.close()
        elapsed = time.monotonic() - began
        stderr = process.stderr.read().decode() if process.stderr else ""
        if process.stderr:
            process.stderr.close()
        if process.stdout:
            process.stdout.close()
        self.assertEqual(returncode, 0, stderr)
        self.assertLess(elapsed, 3.0)
        server.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
