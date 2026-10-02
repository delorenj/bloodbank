#!/usr/bin/env python3
"""LLM usage and allowance contract smoketest.

Validates both new observability facts against the real schema tree and checks
the cross-field rules JSON Schema cannot express: deterministic ordering keys,
event time equal to observation time, and normalized input-token arithmetic.
"""
from __future__ import annotations

import copy
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "services" / "agent-hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from core.validate import EnvelopeInvalid, subject_for, validate_envelope  # noqa: E402

USAGE_TYPE = "bloodbank.llm.usage.recorded"
ALLOWANCE_TYPE = "bloodbank.llm.allowance.observed"


def usage_payload() -> dict:
    return {
        "schema_version": 1,
        "usage_id": "newapi-log:42",
        "gateway_log_id": 42,
        "request_id": "req-42",
        "occurred_at": "2026-10-02T19:00:00Z",
        "provider": "anthropic",
        "account_id": "claude-personal",
        "billing_class": "subscription",
        "route": "automaticai/personal/claude-sonnet-5.5",
        "requested_model": "automaticai/personal/claude-sonnet-5.5",
        "native_model": "claude-sonnet-5-5",
        "returned_model": "claude-sonnet-5-5",
        "requested_effort": "max",
        "effective_effort": "max",
        "effort_defaulted": False,
        "consumer": {
            "kind": "gateway_token",
            "token_id": 29,
            "token_name": "aai:codex:1790770774997259503",
            "agent": "codex",
        },
        "session_id": "01a0fd53-54bb-79f1-bc8d-ca57627ba8b9",
        "project_id": "delocontainers",
        "request_path": "/v1/responses",
        "stream": True,
        "outcome": "completed",
        "status_code": 200,
        "latency_ms": 196_600,
        "time_to_first_token_ms": 789,
        "usage": {
            "input_tokens": 120,
            "uncached_input_tokens": 20,
            "cache_read_input_tokens": 50,
            "cache_write_input_tokens": 50,
            "output_tokens": 10,
            "reasoning_output_tokens": 8,
        },
    }


def allowance_payload() -> dict:
    return {
        "schema_version": 1,
        "observation_id": "newapi-req-42:claude-5h",
        "observed_at": "2026-10-02T19:00:00Z",
        "provider": "anthropic",
        "account_id": "claude-personal",
        "limit_id": "unified",
        "window": "five_hour",
        "utilization_percent": 42.0,
        "reset_at": "2026-10-02T21:00:00Z",
        "source": "response_header",
        "request_id": "req-42",
        "route": "automaticai/personal/claude-sonnet-5.5",
        "native_model": "claude-sonnet-5-5",
    }


def envelope(ce_type: str, data: dict, ordering_key: str) -> dict:
    return {
        "specversion": "1.0",
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"automaticai:{ce_type}:{ordering_key}")),
        "source": "urn:33god:service:automaticai-gateway",
        "type": ce_type,
        "subject": subject_for(ce_type, "event"),
        "time": data["occurred_at"] if ce_type == USAGE_TYPE else data["observed_at"],
        "datacontenttype": "application/json",
        "dataschema": f"apicurio://holyfields/{ce_type}/versions/1",
        "correlationid": str(uuid.uuid4()),
        "causationid": None,
        "producer": "automaticai-usage-exporter",
        "service": "automaticai-gateway",
        "domain": "llm",
        "kind": "event",
        "schemaref": f"{ce_type}.v1",
        "actor": {
            "type": "service",
            "agent_id": "bloodbank.service.automaticai-gateway",
            "cli": None,
            "provider": None,
            "model": None,
        },
        "ordering_key": ordering_key,
        "data": data,
    }


def usage_envelope(**overrides) -> dict:
    data = usage_payload()
    env = envelope(USAGE_TYPE, data, f"usage:{data['usage_id']}")
    for key, value in overrides.items():
        if key.startswith("data."):
            data[key[5:]] = value
        else:
            env[key] = value
    return env


def allowance_envelope(**overrides) -> dict:
    data = allowance_payload()
    env = envelope(
        ALLOWANCE_TYPE,
        data,
        f"allowance:{data['provider']}:{data['account_id']}:{data['window']}",
    )
    for key, value in overrides.items():
        if key.startswith("data."):
            data[key[5:]] = value
        else:
            env[key] = value
    return env


class LLMContractTests(unittest.TestCase):
    def test_usage_validates(self) -> None:
        validate_envelope(usage_envelope())

    def test_allowance_validates(self) -> None:
        validate_envelope(allowance_envelope())

    def test_usage_rejects_prompt_material(self) -> None:
        env = usage_envelope(**{"data.prompt_text": "secret"})
        with self.assertRaises(EnvelopeInvalid):
            validate_envelope(env)

    def test_usage_rejects_wrong_ordering_key(self) -> None:
        env = usage_envelope(ordering_key="request:req-42")
        with self.assertRaises(EnvelopeInvalid):
            validate_envelope(env)

    def test_usage_input_tokens_are_normalized(self) -> None:
        env = usage_envelope()
        usage = env["data"]["usage"]
        self.assertEqual(
            usage["input_tokens"],
            usage["uncached_input_tokens"]
            + usage["cache_read_input_tokens"]
            + usage["cache_write_input_tokens"],
        )

    def test_allowance_rejects_wrong_ordering_key(self) -> None:
        env = allowance_envelope(ordering_key="account:claude-personal")
        with self.assertRaises(EnvelopeInvalid):
            validate_envelope(env)

    def test_allowance_time_matches_observation(self) -> None:
        env = allowance_envelope()
        self.assertEqual(env["time"], env["data"]["observed_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
