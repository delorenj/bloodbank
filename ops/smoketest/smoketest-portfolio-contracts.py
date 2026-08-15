#!/usr/bin/env python3
"""Full-envelope and replay-invariant tests for portfolio orchestration facts."""

from __future__ import annotations

import copy
import json
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
    assert_terminal_receipt_retry,
    portfolio_terminal_receipt_digest,
    subject_for,
    validate_envelope,
)


FIXTURE_PATH = ROOT / "ops" / "fixtures" / "portfolio-contracts.v1.json"
PAYLOADS = json.loads(FIXTURE_PATH.read_text())
RECEIPT_TYPE = "bloodbank.v1.portfolio.receipt.recorded"
EXPECTED_TYPES = {
    "bloodbank.v1.portfolio.intake.received",
    "bloodbank.v1.portfolio.intake.triaged",
    "bloodbank.v1.portfolio.work.delegated",
    "bloodbank.v1.portfolio.work.updated",
    RECEIPT_TYPE,
    "bloodbank.v1.portfolio.approval.requested",
    "bloodbank.v1.portfolio.approval.resolved",
    "bloodbank.v1.portfolio.escalation.raised",
    "bloodbank.v1.portfolio.escalation.resolved",
    "bloodbank.v1.portfolio.capacity.recorded",
    "bloodbank.v1.portfolio.lease.granted",
    "bloodbank.v1.portfolio.lease.released",
    "bloodbank.v1.portfolio.lease.expired",
}
DEFAULT_CAUSATION_ID = "22222222-2222-4222-8222-222222222222"
CORRELATION_ID = "11111111-1111-4111-8111-111111111111"


def envelope(ce_type: str) -> dict:
    data = copy.deepcopy(PAYLOADS[ce_type])
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, ce_type + data["idempotency_key"]))
    causation_id = DEFAULT_CAUSATION_ID
    if ce_type == RECEIPT_TYPE:
        event_id = data["receipt_id"]
        causation_id = data["terminal_event_id"]
    return {
        "specversion": "1.0",
        "id": event_id,
        "source": "urn:33god:agent:delonet-director",
        "type": ce_type,
        "subject": subject_for(ce_type, "event"),
        "time": data["occurred_at"],
        "datacontenttype": "application/json",
        "dataschema": f"apicurio://holyfields/{ce_type}/versions/1",
        "correlationid": CORRELATION_ID,
        "causationid": causation_id,
        "producer": "delonet-director",
        "service": "delonet-director",
        "domain": "portfolio",
        "schemaref": f"{ce_type}.v1",
        "kind": "event",
        "actor": {"type": "agent_api", "agent_id": "delonet-director"},
        "ordering_key": f"portfolio:{data['portfolio_id']}",
        "idempotency_key": data["idempotency_key"],
        "data": data,
    }


class PortfolioContractTests(unittest.TestCase):
    failure_types = (ContractViolation, EnvelopeInvalid)

    def test_fixture_and_schema_family_is_exact(self) -> None:
        self.assertEqual(set(PAYLOADS), EXPECTED_TYPES)
        schema_types = set()
        for path in sorted((ROOT / "schemas" / "bloodbank" / "v1" / "portfolio").glob("*.v1.json")):
            schema = json.loads(path.read_text())
            ce_type = schema["properties"]["type"]["const"]
            schema_types.add(ce_type)
            self.assertEqual(
                schema["properties"]["subject"]["const"],
                subject_for(ce_type, "event"),
            )
        self.assertEqual(schema_types, EXPECTED_TYPES)

    def test_every_fixture_builds_a_valid_complete_envelope(self) -> None:
        for ce_type in sorted(EXPECTED_TYPES):
            with self.subTest(ce_type=ce_type):
                built = envelope(ce_type)
                self.assertEqual(built["data"]["target_agent_id"], PAYLOADS[ce_type]["target_agent_id"])
                self.assertEqual(built["idempotency_key"], built["data"]["idempotency_key"])
                validate_envelope(built)

    def test_identifiers_never_shape_type_or_subject(self) -> None:
        for ce_type in sorted(EXPECTED_TYPES):
            built = envelope(ce_type)
            self.assertEqual(ce_type.split(".")[2], "portfolio")
            self.assertEqual(built["subject"], subject_for(ce_type, "event"))
            for identity in (
                built["data"]["target_agent_id"],
                built["data"].get("work_id"),
                built["data"].get("delegation_id"),
            ):
                if identity:
                    self.assertNotIn(identity, ce_type)
                    self.assertNotIn(identity, built["subject"])

    def test_target_and_idempotency_are_mandatory_and_consistent(self) -> None:
        built = envelope("bloodbank.v1.portfolio.work.delegated")
        missing_target = copy.deepcopy(built)
        missing_target["data"].pop("target_agent_id")
        with self.assertRaises(self.failure_types):
            validate_envelope(missing_target)

        mismatched_key = copy.deepcopy(built)
        mismatched_key["idempotency_key"] = "portfolio.work:conflict"
        with self.assertRaises(ContractViolation):
            assert_contract(mismatched_key)

    def test_capacity_snapshot_arithmetic_is_enforced(self) -> None:
        invalid = envelope("bloodbank.v1.portfolio.capacity.recorded")
        invalid["data"]["capacity_available"] = 2
        with self.assertRaises(ContractViolation):
            assert_contract(invalid)

    def test_terminal_receipt_exact_retry_is_acknowledge_noop(self) -> None:
        original = envelope(RECEIPT_TYPE)
        validate_envelope(original)
        assert_terminal_receipt_retry(original, copy.deepcopy(original))

    def test_terminal_receipt_mutation_cannot_reuse_stale_digest(self) -> None:
        mutated = envelope(RECEIPT_TYPE)
        mutated["data"]["result"]["summary"] = "A different terminal outcome."
        with self.assertRaises(ContractViolation):
            assert_contract(mutated)

    def test_terminal_receipt_conflict_is_never_an_exact_retry(self) -> None:
        original = envelope(RECEIPT_TYPE)
        conflict = copy.deepcopy(original)
        conflict["data"]["terminal_status"] = "failed"
        conflict["data"]["result"]["summary"] = "A conflicting terminal outcome."
        conflict["data"]["result"]["retryable"] = True
        conflict["data"]["outcome_digest"] = portfolio_terminal_receipt_digest(conflict["data"])
        assert_contract(conflict)
        with self.assertRaises(ContractViolation):
            assert_terminal_receipt_retry(original, conflict)

    def test_terminal_receipt_identity_and_causation_are_bound(self) -> None:
        invalid_id = envelope(RECEIPT_TYPE)
        invalid_id["id"] = "66666666-6666-4666-8666-666666666666"
        with self.assertRaises(ContractViolation):
            assert_contract(invalid_id)

        invalid_causation = envelope(RECEIPT_TYPE)
        invalid_causation["causationid"] = DEFAULT_CAUSATION_ID
        with self.assertRaises(ContractViolation):
            assert_contract(invalid_causation)


if __name__ == "__main__":
    unittest.main(verbosity=2)
