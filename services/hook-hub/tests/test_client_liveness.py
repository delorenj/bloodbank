"""The client must not sit out a long deadline on a wedged hub.

A hung hub accepts connections (listen backlog) and never answers. A deadline of
15 s on SessionStart/UserPromptSubmit used to mean 15 s of dead air per hook.
"""
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1] / "client" / "bb-hook"


def run_client(sock_path: str, deadline: str = "15", extra_env: dict | None = None):
    env = {**os.environ, "BB_HOOK_SOCKET": sock_path}
    env.pop("BB_HOOK_HUB", None)
    env.update(extra_env or {})
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(CLIENT), "--cli", "codex", "--native", "UserPromptSubmit",
         "--deadline", deadline],
        input="{}", capture_output=True, text=True, env=env, timeout=30,
    )
    return proc, time.monotonic() - started


class Listener:
    """Unix listener. mode 'hang' accepts and never replies; 'echo' replies at once."""

    def __init__(self, path: str, mode: str, work_delay: float = 0.0):
        self.path, self.mode, self.work_delay = path, mode, work_delay
        self.requests: list[bytes] = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(16)
        self.held: list[socket.socket] = []
        self.stop = False
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        self.sock.settimeout(0.1)
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            if self.mode == "hang":
                self.held.append(conn)
                continue
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        buf = b""
        while True:
            block = conn.recv(65536)
            if not block:
                break
            buf += block
        self.requests.append(buf)
        if b'"op":"ping"' not in buf:
            time.sleep(self.work_delay)  # a busy-but-alive hub: handler work
        conn.sendall(b'{"v":1,"stdout":"hello","exit_code":0,"handled":[]}\n')
        conn.close()

    def close(self):
        self.stop = True
        self.thread.join(timeout=1)
        for c in self.held:
            c.close()
        self.sock.close()


class TestHungHub(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock = str(Path(self.tmp.name) / "hub.sock")

    def tearDown(self):
        self.tmp.cleanup()

    def test_hung_hub_is_bounded_by_probe_not_by_deadline(self):
        hub = Listener(self.sock, "hang")
        try:
            proc, elapsed = run_client(self.sock, "15", {"BB_HOOK_PROBE_BUDGET": "0.6"})
        finally:
            hub.close()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertLess(elapsed, 2.0)
        self.assertGreaterEqual(elapsed, 0.5)

    def test_second_call_after_a_stall_is_shorter(self):
        hub = Listener(self.sock, "hang")
        try:
            run_client(self.sock, "15", {"BB_HOOK_PROBE_BUDGET": "0.8",
                                         "BB_HOOK_STALL_PROBE_BUDGET": "0.2"})
            self.assertTrue(os.path.exists(self.sock + ".stalled"))
            _, elapsed = run_client(self.sock, "15", {"BB_HOOK_PROBE_BUDGET": "0.8",
                                                      "BB_HOOK_STALL_PROBE_BUDGET": "0.2"})
        finally:
            hub.close()
        self.assertLess(elapsed, 0.7)

    def test_busy_but_alive_hub_still_gets_its_full_deadline(self):
        hub = Listener(self.sock, "echo", work_delay=2.5)
        try:
            proc, elapsed = run_client(self.sock, "15", {"BB_HOOK_PROBE_BUDGET": "0.6"})
        finally:
            hub.close()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("hello", proc.stdout)
        self.assertGreaterEqual(elapsed, 2.4)

    def test_recovery_clears_the_stall_marker(self):
        Path(self.sock + ".stalled").touch()
        hub = Listener(self.sock, "echo")
        try:
            proc, _ = run_client(self.sock, "15")
        finally:
            hub.close()
        self.assertIn("hello", proc.stdout)
        self.assertFalse(os.path.exists(self.sock + ".stalled"))

    def test_missing_and_closed_socket_stay_fast_and_exit_zero(self):
        proc, elapsed = run_client(self.sock, "15")
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertLess(elapsed, 1.0)
        closed = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        closed.bind(self.sock)
        closed.close()
        proc, elapsed = run_client(self.sock, "15")
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
