"""Agent State Machine — the properties that must not regress.

These run the REAL asm.lua against a live Redis under a throwaway key prefix,
because the whole correctness argument of this design is "Redis serializes it",
and a mock would test the mock. Skipped cleanly when no Redis is reachable.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

from core import asm                       # noqa: E402
from core.resp import Connection           # noqa: E402

URL = os.environ.get("ASM_TEST_REDIS_URL", "redis://127.0.0.1:6379")
LUA = (SERVICE_DIR / "core" / "asm.lua").read_text()
PREFIX = f"asmtest:{os.getpid()}"


def _redis_available() -> bool:
    try:
        with Connection(URL, timeout=1.0) as conn:
            return conn.command("PING") == "PONG"
    except Exception:
        return False


@unittest.skipUnless(_redis_available(), "no Redis at ASM_TEST_REDIS_URL")
class LuaArbiterTest(unittest.TestCase):
    """The Lua is the serialization point; these are its invariants."""

    def setUp(self) -> None:
        self.scope = f"{PREFIX}:{self.id().rsplit('.', 1)[-1]}"
        self.keys = [f"asm:a:{self.scope}", f"asm:t:{self.scope}",
                     f"asm:lane:{self.scope}", f"asm:live:{self.scope}", ""]
        self._clean()

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with Connection(URL) as conn:
            conn.command("DEL", *[k for k in self.keys if k])

    def fire(self, signal: str, *, lane: str = "main", meta: dict | None = None,
             conn: Connection | None = None) -> object:
        meta = {"cli": "test", "pid": 1, "cwd": "/tmp"} if meta is None else meta
        args = [signal, 900, lane, json.dumps(meta),
                asm.LANE_GRACE_MS, asm.ERR_GRACE_MS, asm.ATTENTION_MS, 500,
                self.scope]
        owns = conn is None
        conn = conn or Connection(URL)
        try:
            return conn.command("EVAL", LUA, len(self.keys), *self.keys, *args)
        finally:
            if owns:
                conn.close()

    def state(self) -> dict[str, str]:
        with Connection(URL) as conn:
            raw = conn.command("HGETALL", self.keys[0])
        return {raw[i]: raw[i + 1] for i in range(0, len(raw), 2)} if raw else {}

    def edges(self) -> list[dict]:
        with Connection(URL) as conn:
            entries = conn.command("XRANGE", self.keys[1], "-", "+")
        out = []
        for entry in entries or []:
            fields = entry[1]
            out.append(json.loads(dict(zip(fields[::2], fields[1::2]))["j"]))
        return out

    # -- the load-bearing property -----------------------------------------

    def test_concurrent_signals_collapse_to_one_edge(self):
        """N racing hooks must produce ONE edge, not N.

        This is the entire reason the reducer can live in the hook process with
        no owning daemon: EVAL is the serialization point, and the counter
        deltas are commutative so arrival order cannot matter.
        """
        self.fire("prompt")
        errors: list[BaseException] = []

        def worker(signal: str) -> None:
            try:
                self.fire(signal)
            except BaseException as exc:      # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=("tool_req",))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(int(self.state()["tools"]), 8)

        threads = [threading.Thread(target=worker, args=("tool_done",))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(int(self.state()["tools"]), 0)

        seq = [(e["from"], e["to"]) for e in self.edges()]
        self.assertEqual(
            seq,
            [("none", "working"), ("working", "tool_running"),
             ("tool_running", "working")],
            f"17 signals should yield 3 edges, got {len(seq)}: {seq}",
        )

    def test_noop_edge_never_escapes(self):
        """A self-transition must return nil, or handlers fire on nothing."""
        self.assertTrue(self.fire("prompt"))
        self.assertIsNone(self.fire("prompt"))
        self.assertEqual(len(self.edges()), 1)

    # -- the traps that were found the hard way -----------------------------

    def test_json_null_in_meta_does_not_abort_the_script(self):
        """cjson decodes null to a TRUTHY lightuserdata, so `x or ''` misses it.

        Before core/asm.lua coerced through s(), one null anywhere in meta
        aborted the whole script with "arguments must be strings or integers"
        and the state write vanished silently behind the fail-open wrapper.
        """
        meta = {k: None for k in
                ("cli", "pid", "starttime", "cwd", "basis", "zellij_session",
                 "zellij_pane", "correlationid", "session_id", "last_role")}
        result = self.fire("prompt", meta=meta)
        self.assertTrue(result)
        self.assertEqual(json.loads(result)["to"], "working")

    def test_orphan_subagent_stop_cannot_underflow(self):
        """claude has NO invocation_start binding, so SubagentStop is orphaned.

        615 invocation.completed against 0 invocation.started in 3 days. A
        naive counter would go negative and wedge the machine.
        """
        self.fire("prompt")
        for _ in range(5):
            self.fire("sub_done", lane="ghost")
        self.assertEqual(int(self.state()["subs"]), 0)
        self.assertEqual(self.state()["state"], "working")

    def test_quiesce_zeroes_counters_so_codex_cannot_wedge(self):
        """codex PostToolUse fires on ~2% of requests (17/734 over 6h).

        Pairing tool_req against tool_done alone strands every codex agent in
        tool_running forever; the unconditional zeroing at a turn boundary is
        the escape hatch, so a dropped signal self-repairs within one turn.
        """
        self.fire("prompt")
        for _ in range(20):
            self.fire("tool_req")             # 20 requests, ZERO completions
        self.assertEqual(self.state()["state"], "tool_running")
        self.fire("quiesce")
        self.assertEqual(self.state()["state"], "idle")
        self.assertEqual(int(self.state()["tools"]), 0)

    def test_first_lane_seen_is_main(self):
        """CLAUDE_CODE_SESSION_ID is set in EVERY child Claude Code spawns —
        hook runners included — so it cannot mean "this is a subagent".

        The machine calibrates instead: the first lane a scope reports is main,
        and only a DIFFERENT lane counts as delegation. Without this, every
        claude agent sits at `delegating` forever.
        """
        self.fire("prompt", lane="session-abc")       # main, whatever it is
        self.assertEqual(self.state()["state"], "working")
        self.assertEqual(int(self.state()["subs"]), 0)

        self.fire("sub_start", lane="session-abc")    # same lane => still main
        self.assertEqual(int(self.state()["subs"]), 0)

        self.fire("sub_start", lane="session-xyz")    # a real second lane
        self.assertEqual(int(self.state()["subs"]), 1)
        self.assertEqual(self.state()["state"], "delegating")

    def test_attention_outranks_everything(self):
        self.fire("prompt")
        self.fire("tool_req")
        self.assertEqual(self.state()["state"], "tool_running")
        self.fire("attention")
        self.assertEqual(self.state()["state"], "awaiting_human")

    def test_every_key_is_bounded(self):
        """maxmemory-policy is noeviction on the box's ONLY Redis, shared with
        the nanoleaf wall and Holocene. An unbounded key is an OOM, not a leak.
        """
        self.fire("prompt")
        self.fire("tool_req")
        with Connection(URL) as conn:
            for key in (self.keys[0], self.keys[1], self.keys[3]):
                self.assertGreater(
                    conn.command("TTL", key), 0, f"{key} has no TTL"
                )


class IdentityLadderTest(unittest.TestCase):
    """Identity must never fall back to the pane; measured, not assumed."""

    def test_proc_stat_parses_a_comm_containing_spaces_and_parens(self):
        got = asm.proc_stat(os.getpid())
        self.assertIsNotNone(got)
        comm, ppid, starttime = got
        self.assertEqual(ppid, os.getppid())
        self.assertGreater(starttime, 0)

    def test_proc_stat_of_a_dead_pid_is_none(self):
        self.assertIsNone(asm.proc_stat(2 ** 31 - 1))

    def test_ladder_falls_to_ppid_when_nothing_else_matches(self):
        scope, basis, _, _ = asm.resolve_scope("no-such-cli", {})
        self.assertEqual(basis, "ppid")
        self.assertTrue(scope.startswith("no-such-cli:x:"))

    def test_ladder_uses_a_native_session_id_for_headless_agents(self):
        """The hermes fleet is 0/1292 paned and its comm never matches, so it
        lands on rung 3 — the rung that keeps 28 PMs from vanishing."""
        scope, basis, _, _ = asm.resolve_scope(
            "no-such-cli", {"session_id": "cron_aec0b33a1d07_x"}
        )
        self.assertEqual(basis, "sid")
        self.assertEqual(scope, "no-such-cli:s:cron_aec0b33a1d07_x")

    def test_scope_carries_starttime_so_pid_reuse_cannot_collide(self):
        scope, _, _, _ = asm.resolve_scope("no-such-cli", {})
        self.assertRegex(scope, r":x:\d+\.\d+$")

    def test_lane_reports_identity_not_judgement(self):
        """_lane must never decide 'this is a subagent' — only asm.lua can.

        The env is cleared explicitly: this suite is itself run BY an agent CLI,
        so CLAUDE_CODE_SESSION_ID is set in the test process and would leak in.
        """
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            self.assertEqual(asm._lane("prompt", {}), "main")
            self.assertEqual(asm._lane("sub_start", {}), "anon")
            self.assertEqual(
                asm._lane("prompt", {"payload": {"agent_id": "codex-sub-1"}}),
                "codex-sub-1",
            )
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-1"}):
            self.assertEqual(asm._lane("prompt", {}), "sess-1")

    def test_failure_detection_reads_the_raw_payload(self):
        self.assertFalse(asm._failed({"tool_name": "Bash"}))
        self.assertTrue(asm._failed({"payload": {"extra": {"error_type": "X"}}}))
        self.assertTrue(asm._failed({"payload": {"extra": {"status": "failed"}}}))
        self.assertTrue(asm._failed({"error": "boom"}))


class RecordFailOpenTest(unittest.TestCase):
    """A hook may never be blocked or slowed by telemetry.

    conftest.py disables the ASM for the whole suite so no test deposits a
    phantom agent into the live table. These tests re-enable it deliberately —
    and point it at a dead port — so "fail-open" is actually exercised rather
    than passing vacuously because the kill switch was on.
    """

    def test_record_swallows_an_unreachable_redis(self):
        with mock.patch.dict(os.environ, {
            "BLOODBANK_ASM": "true",
            "ASM_REDIS_URL": "redis://127.0.0.1:1",   # nothing listens here
        }):
            asm.record("claude", "bloodbank.conversation.turn.started",
                       None, {"prompt": "x"})

    def test_record_ignores_an_unmapped_event_type(self):
        with mock.patch.dict(os.environ, {"BLOODBANK_ASM": "true",
                                          "ASM_REDIS_URL": "redis://127.0.0.1:1"}):
            asm.record("claude", "bloodbank.nope.nope.nope", None, {})

    def test_record_is_a_noop_when_disabled(self):
        with mock.patch.dict(os.environ, {"BLOODBANK_ASM": "false"}):
            asm.record("claude", "bloodbank.conversation.turn.started",
                       None, {"prompt": "x"})


if __name__ == "__main__":
    unittest.main()
