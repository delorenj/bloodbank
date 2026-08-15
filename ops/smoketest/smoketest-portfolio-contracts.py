#!/usr/bin/env python3
"""Full-envelope and replay-invariant tests for portfolio orchestration facts."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "agent-hooks"))
sys.path.insert(0, str(ROOT / "ops" / "smoketest"))

from portfolio_contract import (  # noqa: E402
    PAYLOADS,
    RECEIPT_TYPE,
    build_envelope as envelope,
)

from core.validate import (  # noqa: E402
    ContractViolation,
    EnvelopeInvalid,
    assert_contract,
    assert_terminal_receipt_retry,
    portfolio_terminal_receipt_digest,
    subject_for,
    validate_envelope,
)


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


class PortfolioContractTests(unittest.TestCase):
    failure_types = (ContractViolation, EnvelopeInvalid)

    def test_fixture_and_schema_family_is_exact(self) -> None:
        self.assertEqual(set(PAYLOADS), EXPECTED_TYPES)
        schema_types = set()
        for path in sorted(
            (ROOT / "schemas" / "bloodbank" / "v1" / "portfolio").glob("*.v1.json")
        ):
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
                self.assertEqual(
                    built["data"]["target_agent_id"],
                    PAYLOADS[ce_type]["target_agent_id"],
                )
                self.assertEqual(
                    built["idempotency_key"], built["data"]["idempotency_key"]
                )
                self.assertEqual(
                    built["correlationid"], built["data"]["correlation_id"]
                )
                self.assertEqual(built["causationid"], built["data"]["causation_id"])
                validate_envelope(built)

    def test_root_and_non_root_causation_semantics(self) -> None:
        root = envelope("bloodbank.v1.portfolio.intake.received")
        self.assertIsNone(root["causationid"])
        self.assertIsNone(root["data"]["causation_id"])
        validate_envelope(root)

        for ce_type in sorted(
            EXPECTED_TYPES - {"bloodbank.v1.portfolio.intake.received"}
        ):
            with self.subTest(ce_type=ce_type):
                built = envelope(ce_type)
                self.assertIsNotNone(built["causationid"])
                validate_envelope(built)

    def test_payload_lineage_must_match_envelope(self) -> None:
        built = envelope("bloodbank.v1.portfolio.work.delegated")
        mismatched_correlation = copy.deepcopy(built)
        mismatched_correlation["data"]["correlation_id"] = (
            "33333333-3333-4333-8333-333333333333"
        )
        with self.assertRaises(ContractViolation):
            assert_contract(mismatched_correlation)

        mismatched_causation = copy.deepcopy(built)
        mismatched_causation["data"]["causation_id"] = (
            "33333333-3333-4333-8333-333333333333"
        )
        with self.assertRaises(ContractViolation):
            assert_contract(mismatched_causation)

    def test_only_intake_received_may_be_a_root(self) -> None:
        invalid_root = envelope("bloodbank.v1.portfolio.intake.received")
        invalid_root["causationid"] = "22222222-2222-4222-8222-222222222222"
        invalid_root["data"]["causation_id"] = invalid_root["causationid"]
        with self.assertRaises(self.failure_types):
            validate_envelope(invalid_root)

        invalid_non_root = envelope("bloodbank.v1.portfolio.intake.triaged")
        invalid_non_root["causationid"] = None
        invalid_non_root["data"]["causation_id"] = None
        with self.assertRaises(self.failure_types):
            validate_envelope(invalid_non_root)

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

    def test_terminal_receipt_exact_retry_rejects_schema_invalid_copies(self) -> None:
        invalid = envelope(RECEIPT_TYPE)
        invalid["data"].pop("project")
        with self.assertRaises(EnvelopeInvalid):
            assert_terminal_receipt_retry(invalid, copy.deepcopy(invalid))

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
        conflict["data"]["outcome_digest"] = portfolio_terminal_receipt_digest(
            conflict["data"]
        )
        assert_contract(conflict)
        with self.assertRaises(ContractViolation):
            assert_terminal_receipt_retry(original, conflict)

    def test_terminal_receipt_identity_and_causation_are_bound(self) -> None:
        invalid_id = envelope(RECEIPT_TYPE)
        invalid_id["id"] = "66666666-6666-4666-8666-666666666666"
        with self.assertRaises(ContractViolation):
            assert_contract(invalid_id)

        invalid_causation = envelope(RECEIPT_TYPE)
        invalid_causation["causationid"] = "22222222-2222-4222-8222-222222222222"
        with self.assertRaises(ContractViolation):
            assert_contract(invalid_causation)


if __name__ == "__main__":
    unittest.main(verbosity=2)
