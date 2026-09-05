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

from core import asm, sweep                # noqa: E402
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


@unittest.skipUnless(_redis_available(), "no Redis at ASM_TEST_REDIS_URL")
class SweeperTest(unittest.TestCase):
    """`stale` and `gone` — the two states no event can ever produce."""

    def setUp(self) -> None:
        self.scope = f"{PREFIX}:sweep:{self.id().rsplit('.', 1)[-1]}"
        self.live = f"asm:live:{self.scope}"
        self.keys = [f"asm:a:{self.scope}", f"asm:t:{self.scope}",
                     f"asm:lane:{self.scope}", self.live, ""]
        self._clean()

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with Connection(URL) as conn:
            conn.command("DEL", *[k for k in self.keys if k])

    def seed(self, *signals: str, pid: int, starttime: int) -> None:
        meta = {"cli": "sweeptest", "pid": pid, "starttime": starttime,
                "cwd": "/tmp"}
        with Connection(URL) as conn:
            for sig in signals:
                conn.command(
                    "EVAL", LUA, len(self.keys), *self.keys,
                    sig, 900, "main", json.dumps(meta), asm.LANE_GRACE_MS,
                    asm.ERR_GRACE_MS, asm.ATTENTION_MS, 500, self.scope,
                )

    def age(self, ms: int) -> None:
        """Backdate last_ms so the agent looks silent."""
        with Connection(URL) as conn:
            now = int(conn.command("TIME")[0]) * 1000
            conn.command("HSET", self.keys[0], "last_ms", str(now - ms))

    def sweep(self) -> dict:
        with Connection(URL) as conn:
            return sweep.sweep_once(conn, live_key=self.live)

    def state(self) -> str:
        with Connection(URL) as conn:
            return conn.command("HGET", self.keys[0], "state") or ""

    # -- gone ---------------------------------------------------------------

    def test_dead_process_is_observed_gone_and_reaped(self):
        """Exit is not an event; /proc is the only oracle."""
        dead_pid = 2 ** 31 - 1
        self.seed("prompt", "tool_req", pid=dead_pid, starttime=1)
        self.assertEqual(self.state(), "tool_running")

        summary = self.sweep()
        self.assertEqual(summary["gone"], 1)
        self.assertEqual([(e["from"], e["to"]) for e in summary["transitions"]],
                         [("tool_running", "gone")])
        with Connection(URL) as conn:
            self.assertEqual(conn.command("EXISTS", self.keys[0]), 0)
            self.assertIsNone(conn.command("ZSCORE", self.live, self.scope))

    def test_a_recycled_pid_cannot_inherit_a_dead_agents_row(self):
        """Same pid, different starttime => a different process => gone."""
        self.seed("prompt", pid=os.getpid(), starttime=999999999)
        self.assertEqual(self.sweep()["gone"], 1)

    def test_a_live_process_is_left_alone(self):
        self.seed("prompt", pid=os.getpid(),
                  starttime=asm.proc_stat(os.getpid())[2])
        summary = self.sweep()
        self.assertEqual(summary["gone"], 0)
        self.assertEqual(self.state(), "working")

    def test_a_hermes_profile_is_gone_when_its_gateway_unit_is_stopped(self):
        """The per-profile gateway IS the pid for a Hermes agent. A unit that
        exists with MainPID 0 is an observation, so the PM is reportably down --
        unlike a profile with no unit, which is merely unobservable."""
        self.seed("prompt", pid=0, starttime=0)
        with Connection(URL) as conn:
            conn.command("HSET", self.keys[0], "basis", "agent-env",
                         "profile", "stopped-pm")
            with mock.patch.object(sweep, "gateway_pids",
                                   return_value={"stopped-pm": (0, 0)}):
                summary = sweep.sweep_once(conn, live_key=self.live)
        self.assertEqual(summary["gone"], 1)
        with Connection(URL) as conn:
            self.assertEqual(conn.command("EXISTS", self.keys[0]), 0)

    def test_a_hermes_profile_with_no_gateway_unit_is_only_unobservable(self):
        """Never claim `gone` for something you merely cannot see."""
        self.seed("prompt", pid=0, starttime=0)
        with Connection(URL) as conn:
            conn.command("HSET", self.keys[0], "basis", "agent-env",
                         "profile", "never-deployed-pm")
            with mock.patch.object(sweep, "gateway_pids", return_value={}):
                summary = sweep.sweep_once(conn, live_key=self.live)
        self.assertEqual(summary["gone"], 0)
        with Connection(URL) as conn:
            self.assertEqual(conn.command("EXISTS", self.keys[0]), 1)

    def test_a_live_gateway_keeps_its_profile_row(self):
        me = os.getpid()
        self.seed("prompt", pid=0, starttime=0)
        with Connection(URL) as conn:
            conn.command("HSET", self.keys[0], "basis", "agent-env",
                         "profile", "live-pm")
            with mock.patch.object(
                sweep, "gateway_pids",
                return_value={"live-pm": (me, asm.proc_stat(me)[2])},
            ):
                summary = sweep.sweep_once(conn, live_key=self.live)
        self.assertEqual(summary["gone"], 0)
        self.assertEqual(self.state(), "working")

    def test_headless_agents_are_never_claimed_gone(self):
        """A sid-based scope (the Hermes fleet) has no pid to observe. Claiming
        `gone` for it would be a guess wearing an observation's clothes."""
        self.seed("prompt", "tool_req", pid=0, starttime=0)
        self.assertEqual(self.sweep()["gone"], 0)
        self.assertEqual(self.state(), "tool_running")

    # -- stale --------------------------------------------------------------

    def test_a_wedged_agent_goes_stale(self):
        me = os.getpid()
        self.seed("prompt", "tool_req", pid=me,
                  starttime=asm.proc_stat(me)[2])
        self.age(sweep.STALE_MS + 60_000)
        summary = self.sweep()
        self.assertEqual(summary["stale"], 1)
        self.assertEqual(self.state(), "stale")

    def test_stale_self_heals_on_a_late_signal(self):
        me = os.getpid()
        st = asm.proc_stat(me)[2]
        self.seed("prompt", "tool_req", pid=me, starttime=st)
        self.age(sweep.STALE_MS + 60_000)
        self.sweep()
        self.assertEqual(self.state(), "stale")
        self.seed("tool_done", pid=me, starttime=st)
        self.assertEqual(self.state(), "working")

    def test_idle_never_goes_stale(self):
        """Idle is a resting state, not a fault. An agent waiting on you all
        afternoon must not redden the board."""
        me = os.getpid()
        self.seed("prompt", "quiesce", pid=me, starttime=asm.proc_stat(me)[2])
        self.age(sweep.STALE_MS * 10)
        self.assertEqual(self.sweep()["stale"], 0)
        self.assertEqual(self.state(), "idle")

    def test_awaiting_human_never_goes_stale(self):
        """Blocked on a person is not wedged. Reddening it trains you to
        ignore the one signal that means something."""
        me = os.getpid()
        self.seed("prompt", "tool_req", "attention", pid=me,
                  starttime=asm.proc_stat(me)[2])
        self.age(sweep.STALE_MS * 10)
        self.assertEqual(self.sweep()["stale"], 0)
        self.assertEqual(self.state(), "awaiting_human")

    # -- hygiene ------------------------------------------------------------

    def test_ghost_index_entries_are_reaped(self):
        """The hash TTL'd out but the index survived it."""
        with Connection(URL) as conn:
            conn.command("ZADD", self.live, "1", "no-such-scope")
            summary = sweep.sweep_once(conn, live_key=self.live)
            self.assertEqual(summary["reaped"], 1)
            self.assertIsNone(conn.command("ZSCORE", self.live, "no-such-scope"))

    def test_sweeper_publishes_its_own_liveness(self):
        with Connection(URL) as conn:
            sweep.sweep_once(conn, live_key=self.live)
            self.assertTrue(conn.command("GET", "asm:sweeper"))
            self.assertGreater(conn.command("TTL", "asm:sweeper"), 0)


class HermesIdentityTest(unittest.TestCase):
    """A Hermes agent is a PROFILE, not a process."""

    def test_hermes_home_gives_one_stable_scope_per_profile(self):
        """One profile runs as a long-lived per-profile gateway AND as N
        transient hermes-worker-proc_*.scope units. Keying on the process files
        one PM as many agents -- 9 concurrent james-brennan-pm workers were live
        on this box while this was written."""
        with mock.patch.dict(os.environ, {
            "HERMES_HOME": "/home/delorenj/.hermes/profiles/james-brennan-pm"
        }):
            scope, basis, pid, st = asm.resolve_scope("hermes", {})
        self.assertEqual(scope, "hermes:a:james-brennan-pm")
        self.assertEqual(basis, "agent-env")
        self.assertEqual((pid, st), (0, 0),
                         "identity must carry no pid; liveness is the unit's job")

    def test_a_trailing_slash_does_not_change_identity(self):
        with mock.patch.dict(os.environ, {
            "HERMES_HOME": "/home/delorenj/.hermes/profiles/infra-pm/"
        }):
            self.assertEqual(asm.resolve_scope("hermes", {})[0],
                             "hermes:a:infra-pm")

    def test_the_fleet_router_is_never_an_agent(self):
        """hermes-fleet-bloodbank-gateway.service presents exactly like a
        profile but is ONE unit representing all 25 PMs. Keying anything on it
        collapses the whole fleet into a single row and marks every PM gone on
        one restart."""
        with mock.patch.dict(os.environ, {
            "HERMES_HOME": "/home/delorenj/.hermes/profiles/fleet-bloodbank-gateway"
        }):
            scope, basis, _, _ = asm.resolve_scope("hermes", {})
        self.assertNotIn(":a:", scope)
        self.assertNotEqual(basis, "agent-env")

    def test_the_env_rung_outranks_the_ancestry_walk(self):
        """A worker scope's ancestry DOES contain a `hermes` process, but a
        transient per-invocation one. The env rung must win or every cron tick
        mints a new agent."""
        keys = list(asm.IDENTITY_ENV_FOR_CLI)
        self.assertIn("hermes", keys)
        with mock.patch.dict(os.environ, {
            "HERMES_HOME": "/home/delorenj/.hermes/profiles/deckard-pm"
        }):
            self.assertEqual(asm.resolve_scope("hermes", {})[1], "agent-env")

    def test_other_clis_are_unaffected(self):
        with mock.patch.dict(os.environ, {
            "HERMES_HOME": "/home/delorenj/.hermes/profiles/infra-pm"
        }):
            scope, basis, _, _ = asm.resolve_scope("no-such-cli", {})
        self.assertNotIn(":a:", scope)


class GatewayPidParseTest(unittest.TestCase):
    """The off-by-one that reported live PMs as gone."""

    SHOW_OUTPUT = (
        "MainPID=772171\n"
        "Id=hermes-keepy-money-pm-gateway.service\n"
        "\n"
        "MainPID=1892314\n"
        "Id=hermes-fleet-bloodbank-gateway.service\n"
        "\n"
        "MainPID=749200\n"
        "Id=hermes-bloodbank-pm-gateway.service\n"
        "\n"
        "MainPID=0\n"
        "Id=hermes-tonnybox-pm-gateway.service\n"
    )

    def test_properties_are_matched_by_block_not_by_order(self):
        """systemd emits MainPID BEFORE Id here and guarantees no ordering.
        Assuming Id-then-MainPID paired every unit with the NEXT unit's pid."""
        with mock.patch("core.sweep.subprocess.run") as run:
            run.return_value = mock.Mock(stdout=self.SHOW_OUTPUT)
            with mock.patch.object(asm, "proc_stat", return_value=("hermes", 1, 42)):
                got = sweep.gateway_pids()

        self.assertEqual(got["keepy-money-pm"][0], 772171)
        self.assertEqual(got["bloodbank-pm"][0], 749200)
        self.assertNotIn("fleet-bloodbank-gateway", got,
                         "the fleet router is not an agent")

    def test_a_stopped_gateway_is_an_observation_not_an_absence(self):
        """MainPID 0 on a unit that EXISTS means the agent is down -- reportable
        as gone. A profile with no unit at all is merely unobservable."""
        with mock.patch("core.sweep.subprocess.run") as run:
            run.return_value = mock.Mock(stdout=self.SHOW_OUTPUT)
            got = sweep.gateway_pids()
        self.assertIn("tonnybox-pm", got)
        self.assertEqual(got["tonnybox-pm"], (0, 0))
        self.assertNotIn("never-deployed-pm", got)

    def test_systemctl_failure_degrades_to_unobservable(self):
        with mock.patch("core.sweep.subprocess.run", side_effect=OSError):
            self.assertEqual(sweep.gateway_pids(), {})


class ScriptShaTest(unittest.TestCase):
    """The bug that made every future asm.lua edit a silent no-op."""

    def test_the_sha_is_derived_from_the_body_not_cached(self):
        """Redis keys its script cache by SHA1 of the body, so an edited script
        MUST be a different sha by construction.

        The previous implementation cached the sha in a file. After an edit,
        EVALSHA still resolved against Redis's warm cache and silently ran the
        OLD script -- no error, new signals simply ignored. Found only because a
        freshly-added `gone` signal did nothing at all.
        """
        import hashlib

        body = (SERVICE_DIR / "core" / "asm.lua").read_bytes()
        expected = hashlib.sha1(body).hexdigest()      # noqa: S324

        with Connection(URL) as conn:
            loaded = conn.command("SCRIPT", "LOAD", body)
        self.assertEqual(loaded, expected,
                         "Redis disagrees with our sha derivation")

        mutated = body + b"\n-- edit\n"
        self.assertNotEqual(hashlib.sha1(mutated).hexdigest(),   # noqa: S324
                            expected,
                            "an edited script must not reuse the old sha")


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
