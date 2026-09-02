#!/usr/bin/env python3
"""Project activity contract smoketest.

Proves, against the real schema tree and validator, that
bloodbank.project.activity.recorded behaves as docs/event-naming.md §11.4
says: both audiences validate, the external shape cannot carry internal
facts, the ordering bucket and correlation are bound to the payload, the
window and token arithmetic are enforced, every level refuses unknown
fields, a maximal payload stays under the NATS default max_payload, and
bin/bb-emit builds the same envelope this test builds.

Run: python3 ops/smoketest/smoketest-project-contracts.py
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "services" / "agent-hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from core.validate import (  # noqa: E402
    ContractViolation,
    EnvelopeInvalid,
    assert_contract,
    subject_for,
    validate_envelope,
)

TYPE = "bloodbank.project.activity.recorded"
FIXTURE = ROOT / "ops" / "fixtures" / "project-contracts.v1.json"
FIXTURES = json.loads(FIXTURE.read_text())
BB_EMIT = ROOT / "bin" / "bb-emit"
NATS_MAX_PAYLOAD = 1_048_576  # compose/docker-compose.yml sets no override

failure_types = (ContractViolation, EnvelopeInvalid)


def build_envelope(audience: str, **overrides) -> dict:
    payload = copy.deepcopy(FIXTURES[TYPE][audience])
    env = {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": "urn:33god:skill:activity-report",
        "type": TYPE,
        "subject": subject_for(TYPE, "event"),
        "time": "2026-09-02T07:00:05Z",
        "datacontenttype": "application/json",
        "dataschema": f"apicurio://holyfields/{TYPE}/versions/1",
        "correlationid": payload["generator"]["run_id"],
        "causationid": None,
        "producer": "activity-report",
        "service": "activity-report",
        "domain": "project",
        "kind": "event",
        "schemaref": f"{TYPE}.v1",
        "actor": {
            "type": "service",
            "agent_id": "bloodbank.skill.activity-report",
            "cli": None,
            "provider": None,
            "model": None,
        },
        "ordering_key": f"project:{payload['project']['slug']}",
        "data": payload,
    }
    env.update(overrides)
    return env


def maximal_payload() -> dict:
    """Every cap in the schema hit at once."""
    p = copy.deepcopy(FIXTURES[TYPE]["internal"])
    line = '<p class="row">' + "a" * 60 + "</p>\n"  # quotes + newline exercise JSON escaping
    html = "<!doctype html><html><body>" + line * (262144 // len(line))
    p["report"]["html"] = html[:262144]
    p["report"]["markdown"] = (("- " + "m" * 78 + "\n") * (20000 // 81))[:20000]
    p["report"]["raw"] = (("- " + "r" * 78 + "\n") * (5000 // 81))[:5000]
    p["report"]["title"] = "T" * 180
    repos = [f"repo-{i}" for i in range(8)]
    p["project"]["repos"] = repos
    p["sources"]["git"] = {
        repo: {
            "commits": [
                {
                    "sha": f"{(r * 100) + i:040x}",
                    "subject": "s" * 120,
                    "author": "u" * 80,
                    "at": "2026-09-01T12:00:00Z",
                }
                for i in range(100)
            ],
            "truncated": True,
            "branches": [f"branch-{j}".ljust(120, "b") for j in range(64)],
            "files_changed": 999,
            "insertions": 99999,
            "deletions": 99999,
        }
        for r, repo in enumerate(repos)
    }
    keys = [f"SMK-{i}" for i in range(1, 201)]
    p["sources"]["board"] = {"closed": keys, "opened": keys, "started": keys}
    p["sources"]["candystore"]["by_cli"] = {f"cli{i}": 1000 for i in range(16)}
    p["tickets"] = [
        {
            "key": key,
            "title": "t" * 200,
            "from_state": "f" * 64,
            "to_state": "d" * 64,
            "labels": [f"label-{k}".ljust(40, "l") for k in range(8)],
            "exposure": "internal",
        }
        for key in keys
    ]
    buckets = {
        f"agent{i}": {"input": 1000, "output": 1000, "cache_read": 1000, "cache_write": 1000, "total": 4000}
        for i in range(16)
    }
    p["tokens"] = {"total": 4000 * 16, "by_agent": buckets}
    return p


def bb_emit_check(payload: dict, *extra: str) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, str(BB_EMIT), "--check", "--type", TYPE,
        "--source", "urn:33god:skill:activity-report",
        "--producer", "activity-report", "--service", "activity-report",
        "--actor-type", "service", "--actor-id", "bloodbank.skill.activity-report",
        "--correlation", payload["generator"]["run_id"],
        *extra,
    ]
    return subprocess.run(
        cmd, input=json.dumps(payload), capture_output=True, text=True, cwd=str(ROOT),
        env={**os.environ, "BLOODBANK_SCHEMAS_DIR": str(ROOT / "schemas")},
    )


class ProjectActivityContractTests(unittest.TestCase):
    def test_schema_family_is_exactly_one_file(self):
        files = sorted(p.name for p in (ROOT / "schemas" / "bloodbank" / "project").glob("*.json"))
        self.assertEqual(files, ["activity.recorded.json"])

    def test_both_audiences_validate(self):
        for audience in ("internal", "external"):
            validate_envelope(build_envelope(audience))

    def test_canonical_envelope_fields_are_required(self):
        for field in ("subject", "datacontenttype", "dataschema", "causationid", "schemaref", "ordering_key"):
            env = build_envelope("internal")
            del env[field]
            with self.assertRaises(failure_types, msg=field):
                validate_envelope(env)

    def test_external_cannot_carry_internal_fields(self):
        for field in ("sources", "tickets"):
            env = build_envelope("external")
            env["data"][field] = copy.deepcopy(FIXTURES[TYPE]["internal"][field])
            with self.assertRaises(failure_types, msg=field):
                validate_envelope(env)

    def test_external_text_refuses_ticket_sha_and_path(self):
        cases = {
            "raw ticket": ("raw", " closed SMK-12 today"),
            "raw sha": ("raw", " landed as 43bfa40d"),
            "raw path": ("raw", " see /home/delorenj/code/x"),
            "markdown ticket": ("markdown", "\n- SMK-9 done\n"),
            "title sha": ("title", " 43bfa40d"),
            "html ticket": ("html", "<p>SMK-12</p></html>"),
        }
        for name, (field, suffix) in cases.items():
            env = build_envelope("external")
            env["data"]["report"][field] += suffix
            with self.assertRaises(failure_types, msg=name):
                validate_envelope(env)

    def test_external_html_may_carry_hex_ids(self):
        env = build_envelope("external")
        env["data"]["report"]["html"] = '<!doctype html><html><body id="a1b2c3d4"></body></html>'
        validate_envelope(env)

    def test_internal_text_may_carry_ticket_and_sha(self):
        env = build_envelope("internal")
        self.assertIn("SMK-12", env["data"]["report"]["raw"])
        self.assertIn("43bfa40d", env["data"]["report"]["raw"])
        validate_envelope(env)

    def test_internal_requires_sources_and_tickets(self):
        for field in ("sources", "tickets"):
            env = build_envelope("internal")
            del env["data"][field]
            with self.assertRaises(failure_types, msg=field):
                validate_envelope(env)

    def test_ordering_key_is_bound_to_slug(self):
        for key in ("activity:smoketest-project", "project:other", f"project:{uuid.uuid4()}", "smoketest-project"):
            with self.assertRaises(failure_types, msg=key):
                validate_envelope(build_envelope("internal", ordering_key=key))

    def test_correlation_is_bound_to_run_id(self):
        with self.assertRaises(ContractViolation):
            assert_contract(build_envelope("internal", correlationid=str(uuid.uuid4())))

    def test_window_arithmetic(self):
        env = build_envelope("internal")
        env["data"]["window"]["duration_seconds"] = 86399
        with self.assertRaises(failure_types):
            validate_envelope(env)
        env = build_envelope("internal")
        env["data"]["window"].update({"start": "2026-09-02T07:00:00Z", "end": "2026-09-01T07:00:00Z"})
        with self.assertRaises(failure_types):
            validate_envelope(env)
        env = build_envelope("external")
        env["data"]["window"].update({"end": "2026-09-03T07:00:00Z", "duration_seconds": 172800})
        with self.assertRaises(failure_types):  # cap_24h must be 86400s
            validate_envelope(env)
        env = build_envelope("internal")
        env["data"]["window"]["previous_event_id"] = None
        with self.assertRaises(failure_types):  # previous_report needs a predecessor
            validate_envelope(env)
        env = build_envelope("internal")
        env["data"]["window"].update({"basis": "explicit", "previous_event_id": None})
        validate_envelope(env)

    def test_text_bounds(self):
        cases = {
            "raw 5001": ("raw", "x" * 5001),
            "markdown 20001": ("markdown", "x" * 20001),
            "html 262145": ("html", "<!doctype html>" + "x" * (262145 - 15)),
            "html fragment": ("html", "<div>not a document</div>"),
            "title 181": ("title", "x" * 181),
        }
        for name, (field, value) in cases.items():
            env = build_envelope("internal")
            env["data"]["report"][field] = value
            with self.assertRaises(failure_types, msg=name):
                validate_envelope(env)

    def test_unknown_fields_rejected_at_every_level(self):
        targets = {
            "data": lambda d: d,
            "project": lambda d: d["project"],
            "window": lambda d: d["window"],
            "report": lambda d: d["report"],
            "tokens": lambda d: d["tokens"],
            "bucket": lambda d: d["tokens"]["by_agent"]["claude"],
            "generator": lambda d: d["generator"],
            "sources": lambda d: d["sources"],
            "git": lambda d: d["sources"]["git"]["bloodbank"],
            "commit": lambda d: d["sources"]["git"]["bloodbank"]["commits"][0],
            "candystore": lambda d: d["sources"]["candystore"],
            "board": lambda d: d["sources"]["board"],
            "hindsight": lambda d: d["sources"]["hindsight"],
            "ticket": lambda d: d["tickets"][0],
        }
        for name, pick in targets.items():
            env = build_envelope("internal")
            pick(env["data"])["surprise"] = 1
            with self.assertRaises(failure_types, msg=name):
                validate_envelope(env)

    def test_token_arithmetic(self):
        env = build_envelope("internal")
        env["data"]["tokens"]["by_agent"]["claude"]["total"] += 1
        with self.assertRaises(ContractViolation):
            assert_contract(env)
        env = build_envelope("internal")
        env["data"]["tokens"]["total"] += 1
        with self.assertRaises(ContractViolation):
            assert_contract(env)

    def test_maximal_payload_fits_nats_default(self):
        env = build_envelope("internal", data=maximal_payload())
        validate_envelope(env)
        size = len(json.dumps(env, separators=(",", ":")).encode("utf-8"))
        self.assertLess(size, 0.9 * NATS_MAX_PAYLOAD, f"maximal envelope is {size} bytes")

    def test_bb_emit_check_derives_ordering_key(self):
        payload = FIXTURES[TYPE]["internal"]
        proc = bb_emit_check(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ordering_key  project:smoketest-project", proc.stdout)
        self.assertIn(f"correlationid {payload['generator']['run_id']}", proc.stdout)
        self.assertIn("validated   yes", proc.stdout)
        self.assertIn("schema      yes", proc.stdout)
        self.assertEqual(bb_emit_check(payload, "--ordering-key", "project:smoketest-project").returncode, 0)
        wrong = bb_emit_check(payload, "--ordering-key", "project:other")
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("ordering_key", wrong.stderr)

    def test_bb_emit_check_refuses_external_with_ticket_key(self):
        payload = copy.deepcopy(FIXTURES[TYPE]["external"])
        payload["report"]["raw"] += " (SMK-12)"
        proc = bb_emit_check(payload)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ticket key", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
