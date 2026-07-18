#!/usr/bin/env python3
"""Focused lifecycle schema, kind-resolution, and compatibility regressions."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "agent-hooks"))

from core.envelope import build_envelope  # noqa: E402
from core.validate import (  # noqa: E402
    ContractViolation,
    EnvelopeInvalid,
    load_schema_for,
    validate_envelope,
)


T = "2026-07-18T16:00:00.000Z"
OBSERVATION_ID = "00000000-0000-4000-8000-000000000001"
SOURCE_EVENT_ID = "00000000-0000-4000-8000-000000000002"
RECONCILIATION_ID = "00000000-0000-4000-8000-000000000003"
COMMAND_EVENT_ID = "00000000-0000-4000-8000-000000000005"
COMMAND_ID = "00000000-0000-4000-8000-000000000006"
REPLY_EVENT_ID = "00000000-0000-4000-8000-000000000007"
APPLIED_EVENT_ID = "00000000-0000-4000-8000-000000000008"
CORRELATION_ID = "00000000-0000-4000-8000-000000000009"
ROOT_CAUSATION_ID = "00000000-0000-4000-8000-000000000010"
INVOCATION_ID = "00000000-0000-4000-8000-000000000011"
EVIDENCE_EVENT_ID = "00000000-0000-4000-8000-000000000012"
OBLIGATION_INSTANCE_ID = "00000000-0000-4000-8000-000000000013"

ACTOR = {"type": "service", "agent_id": "delorenj.lifecycle"}


def expect_invalid(label: str, operation: Callable[[], object]) -> None:
    try:
        operation()
    except (ContractViolation, EnvelopeInvalid):
        print(f"  PASS reject {label}")
        return
    raise AssertionError(f"expected rejection: {label}")


def provenance() -> dict:
    return {
        "authority": "delorenj/lifecycle",
        "authority_instance": "lifecycle-test-1",
        "reconciliation_id": RECONCILIATION_ID,
        "policy_version": "1.0.0",
        "source_observation_ids": [OBSERVATION_ID],
    }


def freshness() -> dict:
    return {
        "observed_through": T,
        "evaluated_at": T,
        "status": "fresh",
        "max_age_seconds": 600,
    }


def publication(version: int = 1) -> dict:
    return {
        "outbox_id": version,
        "aggregate_id": "lc-33god",
        "aggregate_version": version,
        "event_sequence": version,
    }


def state(status: str = "active") -> dict:
    return {
        "status": status,
        "health": "nominal",
        "phase": "contracts",
        "progress_percent": 25,
    }


def blocker() -> dict:
    return {
        "id": "blocker-contract-gap",
        "kind": "planning_gap",
        "scope": "lifecycle",
        "blocking": True,
        "summary": "Canonical lifecycle contract was missing",
        "owner_kind": "service",
        "owner_id": "bloodbank",
        "detected_at": T,
        "source_observation_ids": [OBSERVATION_ID],
    }


def event_envelope(
    ce_type: str,
    data: dict,
    event_id: str = SOURCE_EVENT_ID,
    schema_version: int = 1,
) -> dict:
    return build_envelope(
        ce_type=ce_type,
        source="urn:33god:service:lifecycle",
        producer="delorenj/lifecycle",
        service="lifecycle",
        actor=ACTOR,
        data=data,
        correlation_id=CORRELATION_ID,
        causation_id=ROOT_CAUSATION_ID,
        event_id=event_id,
        ordering_key="lifecycle:lc-33god",
        schema_version=schema_version,
        validate=True,
    )


def obligation_evidence_envelope(*, schema_version: int = 2) -> dict:
    data = {
        "contract_version": schema_version,
        "lifecycle_id": "lc-33god",
        "repo": "33GOD",
        "obligation_id": "obligation-review",
        "obligation_kind": "independent_review",
        "target_actor_id": "reviewer",
        "invocation_id": INVOCATION_ID,
        "skill_ref": {
            "name": "bmad-code-review",
            "selector": "6.10.2",
        },
        "completed_at": T,
        "evidence": {
            "kind": "skill_completion",
            "outcome": "completed",
            "artifact_id": "review:lc-33god:obligation-review",
            "artifact_sha256": "b" * 64,
            "summary": "Independent review completed with recorded findings.",
        },
    }
    if schema_version == 2:
        data["obligation_instance_id"] = OBLIGATION_INSTANCE_ID
    return build_envelope(
        ce_type="bloodbank.v1.lifecycle.obligation_evidence.submitted",
        source="urn:33god:service:momo",
        producer="momo",
        service="momo",
        actor={"type": "service", "agent_id": "momo"},
        data=data,
        correlation_id=CORRELATION_ID,
        causation_id=INVOCATION_ID,
        event_id=EVIDENCE_EVENT_ID,
        ordering_key="lifecycle:lc-33god",
        schema_version=schema_version,
        validate=True,
    )


def command_envelope() -> dict:
    return build_envelope(
        ce_type="bloodbank.v1.lifecycle.intent.submit",
        kind="command",
        source="urn:33god:service:holocene",
        producer="holocene",
        service="holocene",
        actor={"type": "service", "agent_id": "holocene"},
        data={
            "contract_version": 1,
            "lifecycle_id": "lc-33god",
            "repo": "33GOD",
            "expected_state_version": 4,
            "intent": {
                "name": "resolve_gate",
                "target": "gate-contract-review",
                "parameters": {"resolution": "approved"},
            },
            "capability": {
                "capability_id": "cap-holocene-resolve",
                "capability_version": 2,
                "action": "lifecycle.intent.submit",
                "scope": "lifecycle:lc-33god",
                "issued_to": "holocene",
            },
            "requested_at": T,
        },
        correlation_id=CORRELATION_ID,
        causation_id=ROOT_CAUSATION_ID,
        event_id=COMMAND_EVENT_ID,
        command_id=COMMAND_ID,
        idempotency_key="lc-33god:resolve-gate:gate-contract-review:v4",
        validate=True,
    )


def reply_envelope(verdict: str) -> dict:
    applied = verdict in {"applied", "idempotent"}
    return build_envelope(
        ce_type="bloodbank.v1.lifecycle.intent.submit",
        kind="reply",
        source="urn:33god:service:lifecycle",
        producer="delorenj/lifecycle",
        service="lifecycle",
        actor=ACTOR,
        data={
            "contract_version": 1,
            "lifecycle_id": "lc-33god",
            "repo": "33GOD",
            "reply_to_command_event_id": COMMAND_EVENT_ID,
            "command_id": COMMAND_ID,
            "idempotency_key": "lc-33god:resolve-gate:gate-contract-review:v4",
            "expected_state_version": 4,
            "observed_state_version": 4,
            "verdict": verdict,
            "mutated": verdict == "applied",
            "resulting_state_version": 5 if applied else None,
            "applied_event_id": APPLIED_EVENT_ID if applied else None,
            "capability_id": (
                None
                if verdict in {"unauthorized", "malformed"}
                else "cap-holocene-resolve"
            ),
            "reason_code": verdict.upper(),
            "responded_at": T,
        },
        correlation_id=CORRELATION_ID,
        causation_id=COMMAND_EVENT_ID,
        event_id=REPLY_EVENT_ID,
        validate=True,
    )


def test_kind_aware_resolution() -> None:
    ce_type = "bloodbank.v1.lifecycle.intent.submit"
    command_schema = load_schema_for(ce_type, "command")
    reply_schema = load_schema_for(ce_type, "reply")
    assert command_schema["properties"]["type"]["const"] == ce_type
    assert reply_schema["properties"]["type"]["const"] == ce_type
    assert command_schema["$id"] != reply_schema["$id"]

    command = command_envelope()
    reply = reply_envelope("applied")
    assert command["type"] == reply["type"] == ce_type
    assert command["kind"] == "command" and reply["kind"] == "reply"
    print("  PASS same CloudEvent type resolves to strict command/reply schemas")

    expect_invalid("ambiguous type-only schema load", lambda: load_schema_for(ce_type))
    expect_invalid("unregistered event kind", lambda: load_schema_for(ce_type, "event"))

    command_as_reply = copy.deepcopy(command)
    command_as_reply.update(
        {
            "kind": "reply",
            "subject": "bloodbank.rpy.v1.lifecycle.intent.submit",
            "dataschema": (
                "apicurio://holyfields/"
                "bloodbank.v1.lifecycle.intent.submit.reply/versions/1"
            ),
            "schemaref": "bloodbank.v1.lifecycle.intent.submit.reply.v1",
        }
    )
    expect_invalid(
        "command payload under reply kind", lambda: validate_envelope(command_as_reply)
    )

    reply_as_command = copy.deepcopy(reply)
    reply_as_command.update(
        {
            "kind": "command",
            "subject": "bloodbank.cmd.v1.lifecycle.intent.submit",
            "dataschema": (
                "apicurio://holyfields/"
                "bloodbank.v1.lifecycle.intent.submit.command/versions/1"
            ),
            "schemaref": "bloodbank.v1.lifecycle.intent.submit.command.v1",
            "command_id": COMMAND_ID,
            "idempotency_key": "lc-33god:resolve-gate:gate-contract-review:v4",
            "delivery": "single_consumer",
        }
    )
    expect_invalid(
        "reply payload under command kind", lambda: validate_envelope(reply_as_command)
    )

    wrong_subject = copy.deepcopy(command)
    wrong_subject["subject"] = "bloodbank.rpy.v1.lifecycle.intent.submit"
    expect_invalid(
        "subject/kind marker mismatch", lambda: validate_envelope(wrong_subject)
    )
    missing_subject = copy.deepcopy(command)
    del missing_subject["subject"]
    expect_invalid(
        "missing subject binding", lambda: validate_envelope(missing_subject)
    )

    wrong_schema_identity = copy.deepcopy(command)
    wrong_schema_identity["dataschema"] = (
        "apicurio://holyfields/bloodbank.v1.lifecycle.intent.submit.reply/versions/1"
    )
    wrong_schema_identity["schemaref"] = "bloodbank.v1.lifecycle.intent.submit.reply.v1"
    expect_invalid(
        "command with reply schema identity",
        lambda: validate_envelope(wrong_schema_identity),
    )


def test_reply_verdicts() -> None:
    for verdict in (
        "accepted",
        "applied",
        "idempotent",
        "stale",
        "unauthorized",
        "malformed",
        "illegal",
    ):
        reply_envelope(verdict)
        print(f"  PASS reply verdict {verdict}")

    for verdict in ("accepted", "stale", "unauthorized", "malformed", "illegal"):
        invalid = reply_envelope(verdict)
        invalid["data"]["mutated"] = True
        expect_invalid(
            f"{verdict} verdict cannot claim mutation",
            lambda e=invalid: validate_envelope(e),
        )

    invalid_idempotent = reply_envelope("idempotent")
    invalid_idempotent["data"]["mutated"] = True
    expect_invalid(
        "idempotent verdict cannot claim a new mutation",
        lambda: validate_envelope(invalid_idempotent),
    )


def test_status_initial_publication() -> None:
    initial = event_envelope(
        "bloodbank.v1.lifecycle.status.updated",
        {
            "contract_version": 1,
            "lifecycle_id": "lc-33god",
            "repo": "33GOD",
            "spec_version": 1,
            "state_version": 1,
            "previous_state_version": None,
            "previous": None,
            "current": state(),
            "transition": {
                "reason": "PROGRESSING",
                "computed": True,
                "detector": "lifecycle@1.0.0",
                "confidence": 1,
            },
            "blockers": [],
            "provenance": provenance(),
            "freshness": freshness(),
            "publication": publication(),
        },
    )
    print("  PASS initial status previous=null at state_version=1")

    empty_repo = copy.deepcopy(initial)
    empty_repo["data"]["repo"] = ""
    expect_invalid(
        "initial status with empty repo", lambda: validate_envelope(empty_repo)
    )

    later_without_previous = copy.deepcopy(initial)
    later_without_previous["data"]["state_version"] = 2
    later_without_previous["data"]["publication"] = publication(2)
    expect_invalid(
        "state_version>1 without prior state",
        lambda: validate_envelope(later_without_previous),
    )

    later = copy.deepcopy(later_without_previous)
    later["data"]["previous_state_version"] = 1
    later["data"]["previous"] = state("planned")
    validate_envelope(later)
    print("  PASS later status requires a prior state and version")


def test_observation_snapshot_and_blocker() -> None:
    source_event = {
        "event_id": SOURCE_EVENT_ID,
        "type": "bloodbank.v1.repo.task.recorded",
        "subject": "bloodbank.evt.v1.repo.task.recorded",
        "source": "urn:33god:integration:n8n",
        "producer": "n8n",
        "ordering_key": "task:33GOD:TASK-42",
        "observed_at": T,
    }
    observation = event_envelope(
        "bloodbank.v1.lifecycle.observation.recorded",
        {
            "contract_version": 1,
            "observation_id": OBSERVATION_ID,
            "lifecycle_id": "lc-33god",
            "repo": "33GOD",
            "observation_kind": "repo_task_event",
            "source_event": source_event,
            "payload_sha256": "a" * 64,
            "payload": {
                "repo": "33GOD",
                "task_id": "TASK-42",
                "title": "Close contracts",
            },
        },
        OBSERVATION_ID,
    )
    print("  PASS observation preserves source identity and deterministic source time")
    missing_time = copy.deepcopy(observation)
    del missing_time["data"]["source_event"]["observed_at"]
    expect_invalid(
        "observation without source event time", lambda: validate_envelope(missing_time)
    )

    event_envelope(
        "bloodbank.v1.lifecycle.blocker.detected",
        {
            "contract_version": 1,
            "lifecycle_id": "lc-33god",
            "repo": "33GOD",
            "spec_version": 1,
            "state_version": 2,
            "previous_state_version": 1,
            "blocker": blocker(),
            "provenance": provenance(),
            "freshness": freshness(),
            "publication": publication(2),
        },
    )
    print("  PASS blocker.detected canonical schema")

    event_envelope(
        "bloodbank.v1.lifecycle.blocker.resolved",
        {
            "contract_version": 1,
            "lifecycle_id": "lc-33god",
            "repo": "33GOD",
            "spec_version": 1,
            "state_version": 3,
            "previous_state_version": 2,
            "blocker": blocker(),
            "resolution": {
                "resolved_at": T,
                "resolved_by": "operator:delorenj",
                "reason_code": "CONTRACT_CLOSED",
            },
            "remaining_blocking_blockers": 0,
            "provenance": provenance(),
            "freshness": freshness(),
            "publication": publication(3),
        },
    )
    print("  PASS blocker.resolved canonical schema")

    snapshot = event_envelope(
        "bloodbank.v1.lifecycle.snapshot.updated",
        {
            "contract_version": 1,
            "lifecycle_id": "lc-33god",
            "repo": "33GOD",
            "spec_version": 1,
            "state_version": 1,
            "previous_state_version": None,
            "state": state(),
            "legal_frontier": [
                {
                    "id": "frontier-resolve-gate",
                    "kind": "command",
                    "action": "lifecycle.intent.submit",
                    "allowed": True,
                    "capability_required": "cap-holocene-resolve",
                    "reason_code": "GATE_OPEN",
                    "expected_state_version": 1,
                }
            ],
            "obligations": [
                {
                    "id": "obligation-review",
                    "kind": "human_review",
                    "status": "pending",
                    "description": "Review the lifecycle contract",
                    "skill_ref": {
                        "name": "bmad-code-review",
                        "selector": "6.10.2",
                    },
                    "owner_id": "operator:delorenj",
                    "due_at": None,
                    "source_observation_ids": [OBSERVATION_ID],
                }
            ],
            "blockers": [blocker()],
            "gates": [
                {
                    "id": "gate-contract-review",
                    "kind": "human_review",
                    "blocking": True,
                    "status": "opened",
                    "reason": "Contract review",
                    "opened_at": T,
                    "resolved_at": None,
                }
            ],
            "capabilities": [
                {
                    "capability_id": "cap-holocene-resolve",
                    "actor_id": "holocene",
                    "actions": ["lifecycle.intent.submit"],
                    "scope": "lifecycle:lc-33god",
                    "issued_at": T,
                    "expires_at": None,
                    "state_version": 1,
                }
            ],
            "provenance": provenance(),
            "freshness": freshness(),
            "publication": publication(),
        },
    )
    print(
        "  PASS snapshot v1 remains compatible without capability_version"
    )

    snapshot_v1_schema = load_schema_for(
        "bloodbank.v1.lifecycle.snapshot.updated", "event", 1
    )
    snapshot_v2_schema = load_schema_for(
        "bloodbank.v1.lifecycle.snapshot.updated", "event", 2
    )
    snapshot_v3_schema = load_schema_for(
        "bloodbank.v1.lifecycle.snapshot.updated", "event", 3
    )
    assert snapshot_v1_schema["$id"].endswith("snapshot.updated.v1.json")
    assert snapshot_v2_schema["$id"].endswith("snapshot.updated.v2.json")
    assert snapshot_v3_schema["$id"].endswith("snapshot.updated.v3.json")

    snapshot_v2_data = copy.deepcopy(snapshot["data"])
    snapshot_v2_data["contract_version"] = 2
    snapshot_v2_data["capabilities"][0]["capability_version"] = 3
    snapshot_v2 = event_envelope(
        "bloodbank.v1.lifecycle.snapshot.updated",
        snapshot_v2_data,
        schema_version=2,
    )
    assert snapshot_v2["schemaref"].endswith(".v2")
    assert snapshot_v2["dataschema"].endswith("/versions/2")
    print("  PASS snapshot v2 requires and carries authority capability_version")

    missing_capability_version = copy.deepcopy(snapshot_v2)
    del missing_capability_version["data"]["capabilities"][0][
        "capability_version"
    ]
    expect_invalid(
        "snapshot v2 without capability_version",
        lambda: validate_envelope(missing_capability_version),
    )

    capability_version_on_v1 = copy.deepcopy(snapshot)
    capability_version_on_v1["data"]["capabilities"][0]["capability_version"] = 3
    expect_invalid(
        "snapshot v1 carrying unversioned capability_version",
        lambda: validate_envelope(capability_version_on_v1),
    )

    snapshot_v3_data = copy.deepcopy(snapshot_v2_data)
    snapshot_v3_data["contract_version"] = 3
    snapshot_v3_data["obligations"][0].update(
        {
            "obligation_instance_id": OBLIGATION_INSTANCE_ID,
            "activated_at": T,
        }
    )
    snapshot_v3 = event_envelope(
        "bloodbank.v1.lifecycle.snapshot.updated",
        snapshot_v3_data,
        schema_version=3,
    )
    assert snapshot_v3["schemaref"].endswith(".v3")
    assert snapshot_v3["dataschema"].endswith("/versions/3")
    print("  PASS snapshot v3 carries one exact authority obligation occurrence")

    for missing in ("obligation_instance_id", "activated_at"):
        invalid_occurrence = copy.deepcopy(snapshot_v3)
        del invalid_occurrence["data"]["obligations"][0][missing]
        expect_invalid(
            f"snapshot v3 obligation without {missing}",
            lambda e=invalid_occurrence: validate_envelope(e),
        )

    expect_invalid(
        "unregistered snapshot schema v4",
        lambda: event_envelope(
            "bloodbank.v1.lifecycle.snapshot.updated",
            snapshot_v3_data,
            schema_version=4,
        ),
    )

    missing_skill_ref = copy.deepcopy(snapshot)
    del missing_skill_ref["data"]["obligations"][0]["skill_ref"]
    expect_invalid(
        "obligation without skill_ref",
        lambda: validate_envelope(missing_skill_ref),
    )

    malformed_skill_name = copy.deepcopy(snapshot)
    malformed_skill_name["data"]["obligations"][0]["skill_ref"]["name"] = (
        "BMAD Code Review"
    )
    expect_invalid(
        "obligation with malformed canonical skill name",
        lambda: validate_envelope(malformed_skill_name),
    )

    empty_skill_selector = copy.deepcopy(snapshot)
    empty_skill_selector["data"]["obligations"][0]["skill_ref"]["selector"] = ""
    expect_invalid(
        "obligation with empty skill selector",
        lambda: validate_envelope(empty_skill_selector),
    )

    embedded_execution_policy = copy.deepcopy(snapshot)
    embedded_execution_policy["data"]["obligations"][0]["skill_ref"]["command"] = (
        "spawn reviewer"
    )
    expect_invalid(
        "obligation skill_ref with embedded execution policy",
        lambda: validate_envelope(embedded_execution_policy),
    )


def test_obligation_completion_evidence() -> None:
    evidence = obligation_evidence_envelope()
    assert evidence["subject"] == (
        "bloodbank.evt.v1.lifecycle.obligation_evidence.submitted"
    )
    assert evidence["schemaref"].endswith(".v2")
    assert evidence["data"]["obligation_instance_id"] == OBLIGATION_INSTANCE_ID
    print("  PASS canonical obligation completion evidence binds exact occurrence")

    legacy = obligation_evidence_envelope(schema_version=1)
    assert legacy["schemaref"].endswith(".v1")
    print("  PASS obligation completion evidence v1 remains schema-compatible")

    missing_occurrence = copy.deepcopy(evidence)
    del missing_occurrence["data"]["obligation_instance_id"]
    expect_invalid(
        "v2 completion evidence without occurrence identity",
        lambda: validate_envelope(missing_occurrence),
    )

    invocation_only = copy.deepcopy(evidence)
    invocation_only["data"]["evidence"]["kind"] = "skill_invocation"
    expect_invalid(
        "skill invocation presented as completion evidence",
        lambda: validate_envelope(invocation_only),
    )

    requested_only = copy.deepcopy(evidence)
    requested_only["data"]["evidence"]["outcome"] = "requested"
    expect_invalid(
        "skill request presented as completion evidence",
        lambda: validate_envelope(requested_only),
    )

    missing_artifact = copy.deepcopy(evidence)
    del missing_artifact["data"]["evidence"]["artifact_sha256"]
    expect_invalid(
        "completion evidence without artifact integrity",
        lambda: validate_envelope(missing_artifact),
    )

    wrong_obligation_subject = copy.deepcopy(evidence)
    wrong_obligation_subject["subject"] = (
        "bloodbank.evt.v1.lifecycle.observation.recorded"
    )
    expect_invalid(
        "completion evidence on wrong subject",
        lambda: validate_envelope(wrong_obligation_subject),
    )


def test_unrelated_legacy_consumer() -> None:
    legacy_type = "bloodbank.v1.agent.session.started"
    schema = load_schema_for(legacy_type)
    assert schema["properties"]["type"]["const"] == legacy_type
    build_envelope(
        ce_type=legacy_type,
        source="urn:33god:agent:test",
        producer="agent-hook",
        service="agent-hooks",
        actor={"type": "agent_cli", "agent_id": "bloodbank.agent.test"},
        data={"session_id": "session-legacy", "working_directory": "/tmp"},
        correlation_id=CORRELATION_ID,
        causation_id=ROOT_CAUSATION_ID,
        ordering_key="session:session-legacy",
        validate=True,
    )
    print("  PASS unrelated legacy type-only consumer remains compatible")


def test_extraction_is_provenance_only() -> None:
    assert not (ROOT / "services" / "lifecycle-controller").exists()
    provenance_doc = (
        ROOT / "docs" / "lifecycle-controller-extraction-provenance.md"
    ).read_text()
    for identity in (
        "03415705a39d77f1e6d73c8a9c92ee177320df7e",
        "ae31b94c31eac6d4f9e7e57cc75b2eb673cbd8d2",
        "36054453f7ee192d7715a1676328c15bfdf89607",
        "delorenj/lifecycle",
    ):
        assert identity in provenance_doc
    print("  PASS embedded controller is absent and immutable provenance remains")


def main() -> None:
    print("lifecycle-contracts: kind-aware command/reply")
    test_kind_aware_resolution()
    print("lifecycle-contracts: stable verdicts")
    test_reply_verdicts()
    print("lifecycle-contracts: initial status publication")
    test_status_initial_publication()
    print("lifecycle-contracts: observation and authority outputs")
    test_observation_snapshot_and_blocker()
    print("lifecycle-contracts: obligation completion evidence")
    test_obligation_completion_evidence()
    print("lifecycle-contracts: unrelated compatibility")
    test_unrelated_legacy_consumer()
    print("lifecycle-contracts: extraction boundary")
    test_extraction_is_provenance_only()
    print("lifecycle-contracts: PASS")


if __name__ == "__main__":
    main()
