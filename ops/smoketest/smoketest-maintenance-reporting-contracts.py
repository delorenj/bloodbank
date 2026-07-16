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
        "failure": {
            "phase": "provider",
            "code": "provider_failed",
            "summary": "All configured maintenance providers failed.",
            "retryable": True,
            "redacted": True,
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
            "sections": {
                "executive-summary": "complete",
                "repo-maintenance": "complete",
            },
        },
        "artifacts": {
            "report_artifact_id": "report:2026-07-15:json",
            "markdown_artifact_id": "report:2026-07-15:markdown",
            "commit_marker_id": "report:2026-07-15:committed",
        },
        "delivery": {
            "status": "delivered",
            "channel": "telegram",
            "destination_alias": "company-owner",
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
            "redacted": True,
        },
        "delivery": {
            "status": "failed",
            "channel": "telegram",
            "destination_alias": "company-owner",
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

    def test_setup_failure_without_provider_is_valid_and_consistent(self) -> None:
        ce_type = "bloodbank.v1.repo.maintenance.failed"
        setup = envelope(ce_type)
        setup["data"]["failure"].update(
            phase="setup",
            code="mirror_unavailable",
            summary="The repository mirror could not be prepared.",
        )
        setup["data"]["outcome"].update(
            provider=None,
            provider_returncode=None,
            provider_status=None,
            merge_attempts=0,
            merge_failures=0,
        )
        validate_envelope(setup)

        contradictory = copy.deepcopy(setup)
        contradictory["data"]["outcome"]["provider"] = "provider-a"
        with self.assertRaises(self.failure_types):
            validate_envelope(contradictory)

        provider_failure = envelope(ce_type)
        provider_failure["data"]["outcome"]["provider"] = None
        with self.assertRaises(self.failure_types):
            validate_envelope(provider_failure)

        merge_failure = envelope(ce_type)
        merge_failure["data"]["failure"].update(
            phase="merge",
            code="merge_rejected",
            summary="The live merge gate rejected the candidate.",
        )
        merge_failure["data"]["outcome"].update(
            provider_returncode=0,
            provider_status="complete",
            merge_attempts=1,
            merge_failures=1,
        )
        validate_envelope(merge_failure)
        merge_failure["data"]["outcome"]["merge_failures"] = 0
        with self.assertRaises(self.failure_types):
            validate_envelope(merge_failure)

    def test_completed_outcomes_reject_contradictory_states(self) -> None:
        maintenance = envelope("bloodbank.v1.repo.maintenance.completed")
        maintenance["data"]["outcome"]["provider_returncode"] = 1
        with self.assertRaises(self.failure_types):
            validate_envelope(maintenance)
        maintenance = envelope("bloodbank.v1.repo.maintenance.completed")
        maintenance["data"]["outcome"]["provider_status"] = "failed"
        with self.assertRaises(self.failure_types):
            validate_envelope(maintenance)

        report = envelope("bloodbank.v1.reporting.report.completed")
        report["data"]["outcome"]["sections"]["executive-summary"] = "degraded"
        with self.assertRaises(self.failure_types):
            validate_envelope(report)

        partial = envelope("bloodbank.v1.reporting.report.completed")
        partial["data"]["outcome"]["status"] = "partial"
        with self.assertRaises(self.failure_types):
            validate_envelope(partial)
        partial["data"]["outcome"]["sections"]["repo-maintenance"] = "degraded"
        validate_envelope(partial)

    def test_delivery_branches_are_coherent(self) -> None:
        completed_type = "bloodbank.v1.reporting.report.completed"
        delivered = envelope(completed_type)
        delivered["data"]["delivery"]["attempts"] = 0
        with self.assertRaises(self.failure_types):
            validate_envelope(delivered)
        delivered = envelope(completed_type)
        delivered["data"]["delivery"]["delivered_at"] = None
        with self.assertRaises(self.failure_types):
            validate_envelope(delivered)
        delivered = envelope(completed_type)
        delivered["data"]["delivery"]["delivered_at"] = "not-a-date-time"
        with self.assertRaises(self.failure_types):
            validate_envelope(delivered)

        skipped = envelope(completed_type)
        skipped["data"]["delivery"] = {
            "status": "skipped",
            "channel": "telegram",
            "destination_alias": "company-owner",
            "attempts": 0,
            "delivered_at": None,
            "reason": "delivery_disabled",
        }
        validate_envelope(skipped)
        skipped["data"]["delivery"]["attempts"] = 1
        with self.assertRaises(self.failure_types):
            validate_envelope(skipped)

        failed_type = "bloodbank.v1.reporting.report.failed"
        not_attempted = envelope(failed_type)
        not_attempted["data"]["delivery"] = {
            "status": "not_attempted",
            "channel": None,
            "destination_alias": None,
            "attempts": 0,
        }
        validate_envelope(not_attempted)
        not_attempted["data"]["delivery"]["attempts"] = 1
        with self.assertRaises(self.failure_types):
            validate_envelope(not_attempted)

        failed = envelope(failed_type)
        failed["data"]["delivery"]["attempts"] = 0
        with self.assertRaises(self.failure_types):
            validate_envelope(failed)

    def test_date_and_datetime_formats_are_enforced(self) -> None:
        for ce_type in PAYLOADS:
            with self.subTest(ce_type=ce_type, field="time"):
                invalid = envelope(ce_type)
                invalid["time"] = "not-a-date-time"
                with self.assertRaises(self.failure_types):
                    validate_envelope(invalid)

            timestamp_field = "at" if ".maintenance." in ce_type else "started_at"
            with self.subTest(ce_type=ce_type, field=timestamp_field):
                invalid = envelope(ce_type)
                invalid["data"][timestamp_field] = "2026-99-99 25:00"
                with self.assertRaises(self.failure_types):
                    validate_envelope(invalid)

            if ".report." in ce_type:
                with self.subTest(ce_type=ce_type, field="report_date"):
                    invalid = envelope(ce_type)
                    invalid["data"]["report_date"] = "2026-99-99"
                    with self.assertRaises(self.failure_types):
                        validate_envelope(invalid)

    def test_private_or_secret_telemetry_is_rejected(self) -> None:
        completed = envelope("bloodbank.v1.reporting.report.completed")
        completed["data"]["artifacts"]["report_artifact_id"] = (
            "/home/operator/private/report.json"
        )
        with self.assertRaises(self.failure_types):
            validate_envelope(completed)

        destination = envelope("bloodbank.v1.reporting.report.completed")
        destination["data"]["delivery"]["destination_alias"] = "-100123456789"
        with self.assertRaises(self.failure_types):
            validate_envelope(destination)

        for ce_type in (
            "bloodbank.v1.repo.maintenance.failed",
            "bloodbank.v1.reporting.report.failed",
        ):
            for summary in (
                "stderr dump contained raw process output",
                "token=exampleSecret123",
                "github_pat_exampleSecret123",
                "failed at /home/operator/.config/secret",
                "credential URL https://user:password@example.invalid",
            ):
                with self.subTest(ce_type=ce_type, summary=summary):
                    invalid = envelope(ce_type)
                    invalid["data"]["failure"]["summary"] = summary
                    with self.assertRaises(self.failure_types):
                        validate_envelope(invalid)

            with self.subTest(ce_type=ce_type, field="stderr"):
                invalid = envelope(ce_type)
                invalid["data"]["failure"]["stderr"] = "raw process output"
                with self.assertRaises(self.failure_types):
                    validate_envelope(invalid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
