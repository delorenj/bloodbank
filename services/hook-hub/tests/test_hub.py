"""hook-hub behavioral tests.

These assert the properties the design actually rests on, not the plumbing:

  * a sync handler's stdout reaches the CLI (that is prompt injection)
  * an async handler runs without the CLI waiting for it
  * require_env skips cleanly instead of failing (bare terminal, no zellij)
  * match_tool filters post_tool by tool name
  * a hung handler is killed and never becomes the caller's problem
  * every malformed input still yields a clean, fail-open reply
  * the client is a no-op when the daemon is absent, and cannot be held by a
    stdin that never closes -- the single most important guarantee here

Run: python3 -m pytest services/hook-hub/tests/test_hub.py
Stdlib + pytest only; no daemon or NATS required (each test starts its own hub).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

HUB_DIR = Path(__file__).resolve().parents[1]
HUB = HUB_DIR / "hub.py"
CLIENT = HUB_DIR / "client" / "bb-hook"


def _free_socket_path(tmp: Path) -> str:
    return str(tmp / "hub.sock")


class HubHarness:
    """Start a hub with a purpose-built registry; tear it down on exit."""

    def __init__(self, tmp: Path, registry: str) -> None:
        self.tmp = tmp
        self.sock = _free_socket_path(tmp)
        self.registry = tmp / "handlers.toml"
        self.registry.write_text(registry)
        self.log = tmp / "hub.log"
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "HubHarness":
        env = dict(
            os.environ,
            BB_HOOK_SOCKET=self.sock,
            HOOK_HUB_REGISTRY=str(self.registry),
            HOOK_HUB_LOG=str(self.log),
            HOOK_HUB_SYNC_BUDGET="2.0",
        )
        self.proc = subprocess.Popen(
            [sys.executable, str(HUB)], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if os.path.exists(self.sock):
                return self
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"hub died: {self.proc.stderr.read().decode(errors='replace')}"
                )
            time.sleep(0.05)
        raise RuntimeError("hub never created its socket")

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def request(self, cli: str, native: str, payload=None,
                env: dict | None = None, timeout: float = 10) -> dict:
        """Speak the wire protocol directly, bypassing the client."""
        req = {"v": 1, "cli": cli, "native": native, "cwd": str(self.tmp),
               "env": env or {}, "payload": payload if payload is not None else {},
               "extra": []}
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(self.sock)
            s.sendall(json.dumps(req).encode() + b"\n")
            s.shutdown(socket.SHUT_WR)
            buf = bytearray()
            while b"\n" not in buf:
                block = s.recv(65536)
                if not block:
                    break
                buf.extend(block)
            return json.loads(bytes(buf).split(b"\n", 1)[0])
        finally:
            s.close()

    def send_raw(self, blob: bytes, timeout: float = 10) -> bytes:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(self.sock)
            s.sendall(blob)
            s.shutdown(socket.SHUT_WR)
            return s.recv(65536)
        finally:
            s.close()


def echo_handler(hid: str, text: str, mode="sync", on='["prompt_submit"]',
                 order=10, extra="") -> str:
    return f"""
[[handler]]
id = "{hid}"
mode = "{mode}"
on = {on}
command = ["/usr/bin/printf", "{text}"]
timeout_ms = 3000
order = {order}
{extra}
"""


class TestDispatch(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_sync_stdout_is_returned(self):
        """A sync handler's stdout is what gets injected into the prompt."""
        with HubHarness(self.tmp, echo_handler("a", "RECALLED")) as h:
            r = h.request("claude", "UserPromptSubmit")
            self.assertEqual(r["stdout"], "RECALLED")
            self.assertEqual(r["handled"], ["a"])
            self.assertEqual(r["exit_code"], 0)

    def test_sync_handlers_compose_in_order(self):
        reg = (echo_handler("second", "SECOND", order=20)
               + echo_handler("first", "FIRST", order=10))
        with HubHarness(self.tmp, reg) as h:
            r = h.request("claude", "UserPromptSubmit")
            self.assertEqual(r["stdout"], "FIRST\n\nSECOND")
            self.assertEqual(r["handled"], ["first", "second"])

    def test_async_handler_runs_without_blocking_reply(self):
        marker = self.tmp / "async.marker"
        reg = f"""
[[handler]]
id = "writer"
mode = "async"
on = ["session_end"]
command = ["/usr/bin/touch", "{marker}"]
timeout_ms = 5000
"""
        with HubHarness(self.tmp, reg) as h:
            r = h.request("claude", "Stop")
            self.assertEqual(r["stdout"], "")        # async output is discarded
            self.assertEqual(r["handled"], ["writer"])
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "async handler never ran")

    def test_require_env_skips_when_absent(self):
        reg = echo_handler(
            "zellij", "MARKED",
            extra='require_env = ["ZELLIJ_PANE_ID", "ZELLIJ_SESSION_NAME"]',
        )
        with HubHarness(self.tmp, reg) as h:
            bare = h.request("claude", "UserPromptSubmit")
            self.assertEqual(bare["handled"], [], "ran without pane context")
            inside = h.request("claude", "UserPromptSubmit",
                               env={"ZELLIJ_PANE_ID": "12",
                                    "ZELLIJ_SESSION_NAME": "Workspace"})
            self.assertEqual(inside["handled"], ["zellij"])
            self.assertEqual(inside["stdout"], "MARKED")

    def test_match_tool_filters_by_tool_name(self):
        reg = echo_handler("retain", "RETAINED", on='["post_tool"]',
                           extra='match_tool = "^(Write|Edit|MultiEdit)$"')
        with HubHarness(self.tmp, reg) as h:
            self.assertEqual(
                h.request("claude", "PostToolUse",
                          payload={"tool_name": "Write"})["handled"], ["retain"])
            self.assertEqual(
                h.request("claude", "PostToolUse",
                          payload={"tool_name": "Bash"})["handled"], [])

    def test_native_only_binding_dispatches(self):
        """Notification has no legal event type but must still reach handlers."""
        reg = """
[[handler]]
id = "notify"
mode = "sync"
on_native = ["Notification"]
command = ["/usr/bin/printf", "DING"]
timeout_ms = 2000
"""
        with HubHarness(self.tmp, reg) as h:
            self.assertEqual(h.request("claude", "Notification")["stdout"], "DING")

    def test_hung_sync_handler_is_killed_and_bounded(self):
        reg = """
[[handler]]
id = "hang"
mode = "sync"
on = ["prompt_submit"]
command = ["/usr/bin/sleep", "60"]
timeout_ms = 700
"""
        with HubHarness(self.tmp, reg) as h:
            t = time.monotonic()
            r = h.request("claude", "UserPromptSubmit", timeout=15)
            elapsed = time.monotonic() - t
            self.assertEqual(r["stdout"], "")
            self.assertLess(elapsed, 6, f"hub held the caller {elapsed:.1f}s")

    def test_missing_binary_is_survivable(self):
        reg = """
[[handler]]
id = "ghost"
mode = "sync"
on = ["prompt_submit"]
command = ["/nonexistent/handler-binary"]
timeout_ms = 1000
"""
        with HubHarness(self.tmp, reg) as h:
            r = h.request("claude", "UserPromptSubmit")
            self.assertEqual(r["stdout"], "")
            self.assertEqual(r["exit_code"], 0)
            # daemon must still be serving afterwards
            self.assertEqual(h.request("claude", "UserPromptSubmit")["exit_code"], 0)

    def test_unknown_binding_is_clean_noop(self):
        with HubHarness(self.tmp, echo_handler("a", "X")) as h:
            r = h.request("nosuchcli", "NoSuchEvent")
            self.assertEqual(r["handled"], [])
            self.assertEqual(r["exit_code"], 0)

    def test_malformed_requests_fail_open(self):
        with HubHarness(self.tmp, echo_handler("a", "X")) as h:
            for blob in (b"not json\n", b"[1,2,3]\n", b"\n", b'{"v":1}\n'):
                reply = json.loads(h.send_raw(blob).split(b"\n", 1)[0])
                self.assertEqual(reply["exit_code"], 0, f"on {blob!r}")
            self.assertEqual(
                h.request("claude", "UserPromptSubmit")["stdout"], "X",
                "daemon degraded after malformed input")

    def test_registry_reload_without_restart(self):
        with HubHarness(self.tmp, echo_handler("a", "BEFORE")) as h:
            self.assertEqual(h.request("claude", "UserPromptSubmit")["stdout"],
                             "BEFORE")
            time.sleep(0.05)
            h.registry.write_text(echo_handler("a", "AFTER"))
            self.assertEqual(h.request("claude", "UserPromptSubmit")["stdout"],
                             "AFTER")

    def test_broken_registry_keeps_last_good_config(self):
        with HubHarness(self.tmp, echo_handler("a", "GOOD")) as h:
            self.assertEqual(h.request("claude", "UserPromptSubmit")["stdout"],
                             "GOOD")
            time.sleep(0.05)
            h.registry.write_text("this is [not valid TOML")
            self.assertEqual(
                h.request("claude", "UserPromptSubmit")["stdout"], "GOOD",
                "a broken registry must not disarm working handlers")


class TestClientFailOpen(unittest.TestCase):
    """The client's contract: it can never wedge or fail an agent."""

    def _run(self, args, stdin=b"{}", env_extra=None, timeout=10):
        env = dict(os.environ, BB_HOOK_SOCKET="/nonexistent/nope.sock")
        env.update(env_extra or {})
        p = subprocess.run([str(CLIENT)] + args, input=stdin,
                           capture_output=True, env=env, timeout=timeout)
        return p

    def test_no_daemon_is_silent_success(self):
        p = self._run(["claude", "PreToolUse"])
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")

    def test_kill_switch(self):
        p = self._run(["claude", "PreToolUse"], env_extra={"BB_HOOK_HUB": "off"})
        self.assertEqual(p.returncode, 0)

    def test_trailer_emitted_even_with_no_daemon(self):
        """Antigravity rejects a hook that does not answer with JSON."""
        for mode, want in (("passive", b"{}"), ("stop", b'{"decision":""}')):
            p = self._run(["antigravity", "Stop", "--trailer", mode])
            self.assertEqual(p.returncode, 0)
            self.assertEqual(p.stdout.strip(), want)

    def test_bad_usage_still_exits_zero(self):
        self.assertEqual(self._run([]).returncode, 0)
        self.assertEqual(self._run(["onlyone"]).returncode, 0)

    def test_non_json_stdin_is_tolerated(self):
        p = self._run(["claude", "PreToolUse"], stdin=b"<<<not json>>>")
        self.assertEqual(p.returncode, 0)

    def test_stdin_that_never_closes_cannot_hang_the_client(self):
        """THE guarantee. A harness holding stdin open must not hold the agent."""
        env = dict(os.environ, BB_HOOK_SOCKET="/nonexistent/nope.sock")
        r, w = os.pipe()                    # write end stays open for the test
        try:
            t = time.monotonic()
            p = subprocess.Popen([str(CLIENT), "claude", "PreToolUse"],
                                 stdin=r, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env)
            os.close(r)
            os.write(w, b'{"tool_name":')    # truncated, then silence forever
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
                self.fail("client hung on a stdin that never closed")
            self.assertEqual(p.returncode, 0)
            self.assertLess(time.monotonic() - t, 5)
        finally:
            os.close(w)

    def test_client_relays_sync_context_end_to_end(self):
        """Full path: client -> socket -> sync handler -> stdout."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with HubHarness(tmp, echo_handler("a", "INJECTED")) as h:
                env = dict(os.environ, BB_HOOK_SOCKET=h.sock)
                p = subprocess.run(
                    [str(CLIENT), "claude", "UserPromptSubmit"],
                    input=b'{"prompt":"hi"}', capture_output=True,
                    env=env, timeout=15,
                )
                self.assertEqual(p.returncode, 0)
                self.assertEqual(p.stdout.strip(), b"INJECTED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
