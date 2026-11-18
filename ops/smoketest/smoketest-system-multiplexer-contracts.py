#!/usr/bin/env python3
"""system.multiplexer contract smoketest.

Proves, against the real schema tree, validator and producer (bin/bb-emit),
that the zellij crash -> resume -> resumed family behaves as
docs/event-naming.md (system, multiplexer) says:

  bloodbank.system.multiplexer.failed   event    root fact of one incident
  bloodbank.system.multiplexer.resume   command  causationid = failed id (trigger=failure)
  bloodbank.system.multiplexer.resumed  event    causationid = command envelope id

- the three schemas exist, and nothing else under that entity;
- bb-emit --check accepts a producer-shaped payload for each, derives
  ordering_key multiplexer:<multiplexer>:<session>, and --print-ids reports
  the envelope id the next link uses as its causationid;
- the resume command's idempotency_key is a function of the correlationid,
  so a re-sent resume for the same incident presents the same key;
- the invariants the schemas carry are enforced (enums, nullable-but-required
  keys, unknown keys refused, RFC 3339 offsets, trigger=failure lineage,
  ordering bucket, tense).

Nothing is published. Run: python3 ops/smoketest/smoketest-system-multiplexer-contracts.py
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "agent-hooks"))

from core.validate import (  # noqa: E402
    ContractViolation,
    EnvelopeInvalid,
    assert_contract,
    subject_for,
    validate_envelope,
)

BB_EMIT = ROOT / "bin" / "bb-emit"
SCHEMA_DIR = ROOT / "schemas" / "bloodbank" / "system"

FAILED = "bloodbank.system.multiplexer.failed"
RESUME = "bloodbank.system.multiplexer.resume"
RESUMED = "bloodbank.system.multiplexer.resumed"
KINDS = {FAILED: "event", RESUME: "command", RESUMED: "event"}

SESSION = "zrtest-contract"
CORRELATION = "11111111-2222-4333-8444-555555555555"
FAILED_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
COMMAND_ENVELOPE_ID = "dddddddd-eeee-4fff-8000-111111111111"

PAYLOADS = {
    FAILED: {
        "multiplexer": "zellij",
        "session": SESSION,
        "host": "big-chungus",
        "server_pid": 12345,
        "server_started_at": "2026-11-18T09:00:00+00:00",
        "detected_at": "2026-11-18T18:30:00.123456+00:00",
        "reason": "panic",
        "panic": {
            "thread": "async-std/runtime",
            "location": "zellij-server/src/tab/mod.rs:123:45",
            "message": "called `Option::unwrap()` on a `None` value",
            "originating": None,
        },
        "log_path": "/tmp/zellij-1000/zellij-log/zellij.log",
        "zellij_version": "zellij 0.44.3",
    },
    RESUME: {
        "multiplexer": "zellij",
        "session": SESSION,
        "trigger": "failure",
        "failure_event_id": FAILED_ID,
        "prompt": None,
        "dry_run": False,
    },
    RESUMED: {
        "multiplexer": "zellij",
        "session": SESSION,
        "outcome": "partial",
        "trigger": "failure",
        "command_id": "cccccccc-dddd-4eee-8fff-000000000000",
        "panes": [
            {
                "pane_id": 3,
                "tab_name": "bloodbank",
                "agent": "claude",
                "agent_source": "command",
                "cwd": "/tmp/zrtest-a",
                "launch": "claude -c 'Zellij crashed ... (sent by zellij-reconstitute)'",
                "status": "launched",
                "error": None,
            },
            {
                "pane_id": 7,
                "tab_name": "infra",
                "agent": None,
                "agent_source": None,
                "cwd": None,
                "launch": None,
                "status": "failed",
                "error": "pane shell not found",
            },
        ],
        "duration_ms": 4120,
        "error": None,
    },
}

CAUSATION = {FAILED: None, RESUME: FAILED_ID, RESUMED: COMMAND_ENVELOPE_ID}


def build_envelope(ce_type: str, data: dict | None = None, **overrides) -> dict:
    """The envelope bin/bb-emit builds for this family (same field set and derivations)."""
    kind = KINDS[ce_type]
    data = copy.deepcopy(PAYLOADS[ce_type] if data is None else data)
    env = {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": "urn:33god:service:zellij-sentinel",
        "type": ce_type,
        "subject": subject_for(ce_type, kind),
        "time": "2026-11-18T18:30:01Z",
        "datacontenttype": "application/json",
        "correlationid": CORRELATION,
        "causationid": CAUSATION[ce_type],
        "producer": "zellij-sentinel",
        "service": "zellij-sentinel",
        "domain": "system",
        "schemaref": f"{ce_type}.v1",
        "dataschema": f"apicurio://holyfields/{ce_type}/versions/1",
        "kind": kind,
        "actor": {"type": "service", "agent_id": "bloodbank.service.zellij-sentinel",
                  "cli": None, "provider": None, "model": None},
        "ordering_key": f"multiplexer:zellij:{SESSION}",
        "data": data,
    }
    if kind == "command":
        env["command_id"] = str(uuid.uuid4())
        env["idempotency_key"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ce_type}:{CORRELATION}"))
        env["delivery"] = "single_consumer"
    env.update(overrides)
    return env


def bb_emit_check(ce_type: str, data: dict, *extra: str) -> subprocess.CompletedProcess:
    type_arg = subject_for(ce_type, KINDS[ce_type]) if KINDS[ce_type] == "command" else ce_type
    args = [sys.executable, str(BB_EMIT), "--check", "--print-ids", "--type", type_arg,
            "--service", "zellij-sentinel", "--correlation", CORRELATION, *extra]
    if CAUSATION[ce_type]:
        args += ["--causation", CAUSATION[ce_type]]
    return subprocess.run(args, input=json.dumps(data), capture_output=True, text=True, timeout=60)


def ids_line(proc: subprocess.CompletedProcess) -> dict:
    return json.loads(proc.stdout.strip().splitlines()[-1])


class MultiplexerContractTests(unittest.TestCase):
    failure_types = (ContractViolation, EnvelopeInvalid)

    def refuse(self, envelope: dict, msg: str) -> None:
        with self.assertRaises(self.failure_types, msg=msg):
            validate_envelope(envelope)

    # -- family ---------------------------------------------------------------

    def test_schema_family_is_exact(self) -> None:
        found = {}
        for path in sorted(SCHEMA_DIR.glob("multiplexer.*.json")):
            schema = json.loads(path.read_text())
            props = schema["properties"]
            ce_type = props["type"]["const"]
            found[ce_type] = props["kind"]["const"]
            self.assertEqual(path.name, ce_type.split(".", 2)[2] + ".json")
            self.assertEqual(props["domain"]["const"], "system")
            self.assertEqual(props["subject"]["const"], subject_for(ce_type, found[ce_type]))
            self.assertEqual(schema["$id"],
                             f"https://33god.dev/schemas/bloodbank/system/{path.name}")
        self.assertEqual(found, KINDS)

    def test_every_fixture_builds_a_valid_envelope(self) -> None:
        for ce_type in KINDS:
            with self.subTest(ce_type=ce_type):
                validate_envelope(build_envelope(ce_type))

    # -- the real producer ----------------------------------------------------

    def test_bb_emit_check_accepts_every_fixture_and_reports_ids(self) -> None:
        for ce_type, data in PAYLOADS.items():
            with self.subTest(ce_type=ce_type):
                proc = bb_emit_check(ce_type, data)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("schema      yes", proc.stdout)
                ids = ids_line(proc)
                uuid.UUID(ids["id"])  # the envelope id the next link uses as causationid
                self.assertEqual(ids["type"], ce_type)
                self.assertEqual(ids["correlationid"], CORRELATION)
                self.assertEqual(ids["ordering_key"], f"multiplexer:zellij:{SESSION}")
                if KINDS[ce_type] == "command":
                    uuid.UUID(ids["command_id"])
                    self.assertTrue(ids["idempotency_key"])
                else:
                    self.assertIsNone(ids["command_id"])

    def test_resume_idempotency_key_follows_the_incident(self) -> None:
        a = ids_line(bb_emit_check(RESUME, PAYLOADS[RESUME]))
        b = ids_line(bb_emit_check(RESUME, PAYLOADS[RESUME]))
        self.assertNotEqual(a["command_id"], b["command_id"])
        self.assertEqual(a["idempotency_key"], b["idempotency_key"])
        self.assertEqual(
            a["idempotency_key"],
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"{RESUME}:{CORRELATION}")),
        )

    def test_bb_emit_check_refuses_a_drifted_payload(self) -> None:
        data = copy.deepcopy(PAYLOADS[RESUMED])
        data["panes"][0]["status"] = "relaunched"
        proc = bb_emit_check(RESUMED, data)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("JSON Schema", proc.stderr)

    # -- failed ---------------------------------------------------------------

    def test_failed_variants(self) -> None:
        absent = copy.deepcopy(PAYLOADS[FAILED])
        absent.update(reason="server_absent_at_start", server_pid=0, server_started_at=None,
                      panic=None, log_path=None, zellij_version=None)
        validate_envelope(build_envelope(FAILED, absent))
        for reason in ("oom_killed", "unknown"):
            data = copy.deepcopy(PAYLOADS[FAILED])
            data.update(reason=reason, panic=None)
            validate_envelope(build_envelope(FAILED, data))

    def test_failed_refusals(self) -> None:
        cases = {
            "unknown reason": {"reason": "crashed"},
            "other multiplexer": {"multiplexer": "tmux"},
            "naive detected_at": {"detected_at": "2026-11-18T18:30:00"},
            "negative pid": {"server_pid": -1},
            "empty session": {"session": ""},
            "unknown key": {"summary": "zellij died"},
        }
        for name, patch in cases.items():
            with self.subTest(name=name):
                data = copy.deepcopy(PAYLOADS[FAILED])
                data.update(patch)
                self.refuse(build_envelope(FAILED, data), name)
        for key in PAYLOADS[FAILED]:
            with self.subTest(missing=key):
                data = copy.deepcopy(PAYLOADS[FAILED])
                del data[key]
                self.refuse(build_envelope(FAILED, data), f"missing {key}")
        data = copy.deepcopy(PAYLOADS[FAILED])
        del data["panic"]["originating"]
        self.refuse(build_envelope(FAILED, data), "panic missing originating")

    # -- resume ---------------------------------------------------------------

    def test_resume_roots_and_minimal_payload(self) -> None:
        for trigger in ("boot", "manual"):
            with self.subTest(trigger=trigger):
                data = {"multiplexer": "zellij", "session": "Workspace", "trigger": trigger}
                validate_envelope(build_envelope(RESUME, data, causationid=None))

    def test_resume_failure_lineage_is_required(self) -> None:
        self.refuse(build_envelope(RESUME, causationid=None), "trigger=failure without causationid")
        data = copy.deepcopy(PAYLOADS[RESUME])
        data["failure_event_id"] = None
        self.refuse(build_envelope(RESUME, data), "trigger=failure with null failure_event_id")
        del data["failure_event_id"]
        self.refuse(build_envelope(RESUME, data), "trigger=failure without failure_event_id")

    def test_resume_refusals(self) -> None:
        cases = {
            "unknown trigger": {"trigger": "cron"},
            "dry_run not bool": {"dry_run": "yes"},
            "unknown key": {"panes": [3]},
        }
        for name, patch in cases.items():
            with self.subTest(name=name):
                data = copy.deepcopy(PAYLOADS[RESUME])
                data.update(patch)
                self.refuse(build_envelope(RESUME, data), name)
        for key in ("multiplexer", "session", "trigger"):
            with self.subTest(missing=key):
                data = copy.deepcopy(PAYLOADS[RESUME])
                del data[key]
                self.refuse(build_envelope(RESUME, data), f"missing {key}")
        env = build_envelope(RESUME)
        del env["idempotency_key"]
        self.refuse(env, "command without idempotency_key")

    # -- resumed --------------------------------------------------------------

    def test_resumed_variants(self) -> None:
        noop = {"multiplexer": "zellij", "session": SESSION, "outcome": "noop", "trigger": "manual",
                "command_id": None, "panes": [], "duration_ms": 0, "error": "already running"}
        validate_envelope(build_envelope(RESUMED, noop))
        data = copy.deepcopy(PAYLOADS[RESUMED])
        data["panes"][0].update(agent="kimi", agent_source="marker", status="skipped",
                                error="not held")
        data["panes"][1].update(agent="claude", agent_source="default")
        validate_envelope(build_envelope(RESUMED, data))

    def test_resumed_refusals(self) -> None:
        cases = {
            "unknown outcome": {"outcome": "done"},
            "negative duration": {"duration_ms": -5},
            "panes not a list": {"panes": {}},
            "unknown key": {"host": "big-chungus"},
        }
        for name, patch in cases.items():
            with self.subTest(name=name):
                data = copy.deepcopy(PAYLOADS[RESUMED])
                data.update(patch)
                self.refuse(build_envelope(RESUMED, data), name)
        pane_cases = {
            "unknown pane status": {"status": "relaunched"},
            "unknown agent_source": {"agent_source": "guess"},
            "string pane_id": {"pane_id": "terminal_3"},
            "unknown pane key": {"is_held": True},
        }
        for name, patch in pane_cases.items():
            with self.subTest(name=name):
                data = copy.deepcopy(PAYLOADS[RESUMED])
                data["panes"][0].update(patch)
                self.refuse(build_envelope(RESUMED, data), name)
        for key in PAYLOADS[RESUMED]["panes"][0]:
            with self.subTest(missing_pane_key=key):
                data = copy.deepcopy(PAYLOADS[RESUMED])
                del data["panes"][0][key]
                self.refuse(build_envelope(RESUMED, data), f"pane missing {key}")

    # -- envelope -------------------------------------------------------------

    def test_ordering_bucket_and_tense(self) -> None:
        for ce_type in KINDS:
            with self.subTest(ce_type=ce_type):
                self.refuse(build_envelope(ce_type, ordering_key=f"system:{uuid.uuid4()}"),
                            "ordering_key outside multiplexer:<multiplexer>:<session>")
        with self.assertRaises(ContractViolation):
            assert_contract(build_envelope(RESUME, kind="event", subject=subject_for(RESUME, "event")))
        with self.assertRaises(ContractViolation):
            assert_contract(build_envelope(FAILED, kind="command", subject=subject_for(FAILED, "command"),
                                           command_id=str(uuid.uuid4()), idempotency_key="k",
                                           delivery="single_consumer"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
