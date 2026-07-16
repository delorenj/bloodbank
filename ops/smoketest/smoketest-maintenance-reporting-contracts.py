#!/usr/bin/env python3
"""Full-envelope tests for repo maintenance and company reporting events."""

from __future__ import annotations

import copy
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "agent-hooks"))

from core.validate import (  # noqa: E402
    ContractViolation,
    EnvelopeInvalid,
    validate_envelope,
)


PAYLOADS = {
    "bloodbank.v1.repo.maintenance.started": {
        "schema_version": 1,
        "run_id": "tick-000001-20260715T060000Z",
        "repository": "delorenj/mcp-server-trello",
        "tick": 1,
        "at": "2026-07-15T06:00:00Z",
        "automerge": False,
        "outcome": {"status": "started", "success": None},
    },
    "bloodbank.v1.repo.maintenance.completed": {
        "schema_version": 1,
        "run_id": "tick-000001-20260715T060000Z",
        "repository": "delorenj/mcp-server-trello",
        "tick": 1,
        "at": "2026-07-15T06:05:00Z",
        "automerge": False,
        "outcome": {
            "status": "completed",
            "success": True,
            "provider": "provider-a",
            "provider_returncode": 0,
            "provider_status": "no_work",
            "merge_attempts": 0,
            "merge_failures": 0,
        },
    },
    "bloodbank.v1.repo.maintenance.failed": {
        "schema_version": 1,
        "run_id": "tick-000002-20260715T061000Z",
        "repository": "delorenj/mcp-server-trello",
        "tick": 2,
        "at": "2026-07-15T06:15:00Z",
        "automerge": True,
        "outcome": {
            "status": "failed",
            "success": False,
            "provider": "provider-b",
            "provider_returncode": 1,
            "provider_status": "failed",
            "merge_attempts": 0,
            "merge_failures": 0,
        },
    },
    "bloodbank.v1.reporting.report.started": {
        "schema_version": 1,
        "run_id": "daily-2026-07-15",
        "report_date": "2026-07-15",
        "started_at": "2026-07-15T10:55:00Z",
        "timezone": "America/New_York",
        "trigger": "scheduled",
        "expected_sections": ["executive-summary", "repo-maintenance"],
    },
    "bloodbank.v1.reporting.report.completed": {
        "schema_version": 1,
        "run_id": "daily-2026-07-15",
        "report_date": "2026-07-15",
        "started_at": "2026-07-15T10:55:00Z",
        "completed_at": "2026-07-15T11:00:00Z",
        "outcome": {
            "status": "complete",
            "sections_expected": 2,
            "sections_complete": 2,
            "sections_degraded": 0,
        },
        "artifacts": {
            "report_json_path": "/var/lib/delonet-daily-report/2026-07-15.report.json",
            "markdown_path": "/var/lib/delonet-daily-report/2026-07-15.md",
            "commit_marker_path": (
                "/var/lib/delonet-daily-report/2026-07-15.committed.json"
            ),
        },
        "delivery": {
            "status": "delivered",
            "channel": "telegram",
            "destination": "company-owner",
            "attempts": 1,
            "delivered_at": "2026-07-15T11:00:00Z",
        },
    },
    "bloodbank.v1.reporting.report.failed": {
        "schema_version": 1,
        "run_id": "daily-2026-07-16",
        "report_date": "2026-07-16",
        "started_at": "2026-07-16T10:55:00Z",
        "failed_at": "2026-07-16T11:00:00Z",
        "failure": {
            "phase": "delivery",
            "code": "delivery_unavailable",
            "summary": "The configured delivery channel was unavailable.",
            "retryable": True,
        },
        "delivery": {
            "status": "failed",
            "channel": "telegram",
            "destination": "company-owner",
            "attempts": 3,
        },
    },
}


def envelope(ce_type: str) -> dict:
    suffix = ce_type.split(".", 2)[2]
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, ce_type))
    return {
        "specversion": "1.0",
        "id": event_id,
        "source": "urn:33god:service:contract-smoketest",
        "type": ce_type,
        "subject": f"bloodbank.evt.v1.{suffix}",
        "time": "2026-07-15T11:00:00Z",
        "datacontenttype": "application/json",
        "dataschema": f"apicurio://holyfields/{ce_type}/versions/1",
        "correlationid": event_id,
        "causationid": event_id,
        "producer": "contract-smoketest",
        "service": "contract-smoketest",
        "domain": ce_type.split(".")[2],
        "schemaref": f"{ce_type}.v1",
        "kind": "event",
        "actor": {
            "type": "service",
            "agent_id": "bloodbank.test.maintenance-reporting",
        },
        "ordering_key": f"run:{PAYLOADS[ce_type]['run_id']}",
        "data": copy.deepcopy(PAYLOADS[ce_type]),
    }


class MaintenanceReportingContractTests(unittest.TestCase):
    failure_types = (ContractViolation, EnvelopeInvalid)

    def test_each_complete_envelope_is_valid(self) -> None:
        for ce_type in PAYLOADS:
            with self.subTest(ce_type=ce_type):
                validate_envelope(envelope(ce_type))

    def test_each_envelope_rejects_missing_canonical_fields(self) -> None:
        for ce_type in PAYLOADS:
            for field in ("subject", "actor", "ordering_key", "causationid"):
                with self.subTest(ce_type=ce_type, field=field):
                    invalid = envelope(ce_type)
                    invalid.pop(field)
                    with self.assertRaises(self.failure_types):
                        validate_envelope(invalid)

    def test_each_envelope_rejects_wrong_contract_bindings(self) -> None:
        mutations = {
            "subject": "bloodbank.evt.v1.system.heartbeat.received",
            "dataschema": "apicurio://holyfields/wrong/versions/1",
            "schemaref": "bloodbank.v1.system.heartbeat.received.v1",
        }
        for ce_type in PAYLOADS:
            for field, value in mutations.items():
                with self.subTest(ce_type=ce_type, field=field):
                    invalid = envelope(ce_type)
                    invalid[field] = value
                    with self.assertRaises(self.failure_types):
                        validate_envelope(invalid)

    def test_each_envelope_rejects_malformed_data(self) -> None:
        for ce_type in PAYLOADS:
            with self.subTest(ce_type=ce_type, problem="unknown-field"):
                invalid = envelope(ce_type)
                invalid["data"]["unexpected"] = True
                with self.assertRaises(self.failure_types):
                    validate_envelope(invalid)
            with self.subTest(ce_type=ce_type, problem="missing-required"):
                invalid = envelope(ce_type)
                invalid["data"].pop("schema_version")
                with self.assertRaises(self.failure_types):
                    validate_envelope(invalid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
