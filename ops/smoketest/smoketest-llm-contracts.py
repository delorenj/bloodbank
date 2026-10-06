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

    def test_allowance_legacy_windows_and_limits_validate_without_mutation(self) -> None:
        for window in ("five_hour", "daily", "weekly", "monthly"):
            for limit_id in (None, "unified"):
                with self.subTest(window=window, limit_id=limit_id):
                    env = allowance_envelope(**{
                        "data.window": window,
                        "data.limit_id": limit_id,
                        "ordering_key": f"allowance:anthropic:claude-personal:{window}",
                    })
                    original = copy.deepcopy(env)
                    validate_envelope(env)
                    self.assertEqual(env, original)
                    self.assertEqual(env["data"]["schema_version"], 1)
                    self.assertEqual(env["schemaref"], f"{ALLOWANCE_TYPE}.v1")
                    self.assertEqual(
                        env["dataschema"], f"apicurio://holyfields/{ALLOWANCE_TYPE}/versions/1"
                    )

    def test_allowance_custom_window_validates(self) -> None:
        for seconds in (1, 10_800, 9007199254740991):
            with self.subTest(seconds=seconds):
                validate_envelope(allowance_envelope(**{
                    "data.window": "custom",
                    "data.window_seconds": seconds,
                    "ordering_key": "allowance:anthropic:claude-personal:custom",
                }))

    def test_allowance_rejects_invalid_window_seconds(self) -> None:
        for seconds in (0, -1, 1.5, 9007199254740992, None, True, "10800"):
            with self.subTest(seconds=seconds):
                env = allowance_envelope(**{"data.window_seconds": seconds})
                with self.assertRaises(EnvelopeInvalid):
                    validate_envelope(env)

    def test_allowance_named_window_accepts_optional_duration(self) -> None:
        validate_envelope(allowance_envelope(**{"data.window_seconds": 18_000}))

    def test_allowance_quota_units_and_partial_amounts_validate(self) -> None:
        for unit in ("tokens", "requests", "credits", "tool_calls"):
            for field in ("quota_limit", "quota_used", "quota_remaining"):
                for amount in (0, 0.5, 9007199254740991):
                    with self.subTest(unit=unit, field=field, amount=amount):
                        validate_envelope(allowance_envelope(**{
                            "data.quota_unit": unit,
                            f"data.{field}": amount,
                        }))

    def test_allowance_remaining_only_validates(self) -> None:
        validate_envelope(allowance_envelope(**{
            "data.quota_unit": "requests",
            "data.quota_remaining": 75,
        }))

    def test_allowance_complete_quota_validates(self) -> None:
        validate_envelope(allowance_envelope(**{
            "data.quota_unit": "credits",
            "data.quota_limit": 100.5,
            "data.quota_used": 25.25,
            "data.quota_remaining": 75.25,
        }))

    def test_allowance_quota_unit_does_not_require_amounts(self) -> None:
        validate_envelope(allowance_envelope(**{"data.quota_unit": "tokens"}))

    def test_allowance_each_quota_amount_requires_unit(self) -> None:
        for field in ("quota_limit", "quota_used", "quota_remaining"):
            with self.subTest(field=field):
                env = allowance_envelope(**{f"data.{field}": 0})
                with self.assertRaises(EnvelopeInvalid):
                    validate_envelope(env)

    def test_allowance_rejects_invalid_quota_amounts(self) -> None:
        for field in ("quota_limit", "quota_used", "quota_remaining"):
            for amount in (-1, -0.5, 9007199254740992, None, True, "75"):
                with self.subTest(field=field, amount=amount):
                    env = allowance_envelope(**{
                        "data.quota_unit": "requests",
                        f"data.{field}": amount,
                    })
                    with self.assertRaises(EnvelopeInvalid):
                        validate_envelope(env)

    def test_allowance_rejects_unknown_quota_units(self) -> None:
        for unit in ("usd", "token", "", None, 1):
            with self.subTest(unit=unit):
                env = allowance_envelope(**{"data.quota_unit": unit})
                with self.assertRaises(EnvelopeInvalid):
                    validate_envelope(env)

    def test_allowance_applies_to_shared_account_and_canonical_routes(self) -> None:
        for routes in ([], [
            "automaticai/personal/claude-sonnet-5.5",
            "automaticai/intelliforia/claude-opus-5.5",
            "automaticai/personal/gpt-5.5",
            "automaticai/personal/kimi-k3",
            "automaticai/personal/glm-5.3",
            "automaticai/openrouter/~vendor/family/model:free+variant",
        ]):
            with self.subTest(routes=routes):
                validate_envelope(allowance_envelope(**{"data.applies_to": routes}))

    def test_allowance_applies_to_accepts_item_and_length_boundaries(self) -> None:
        prefix = "automaticai/personal/"
        longest_route = prefix + "x" * (255 - len(prefix))
        routes = [f"automaticai/personal/model-{index}" for index in range(99)]
        routes.append(longest_route)
        validate_envelope(allowance_envelope(**{"data.applies_to": routes}))

    def test_allowance_rejects_invalid_applies_to(self) -> None:
        route = "automaticai/personal/gpt-5.5"
        invalid = [
            None, route, [None], [1], [""], [route, route],
            [f"automaticai/personal/model-{index}" for index in range(101)],
            ["automaticai/personal/" + "x" * 235],
            ["aai/personal/gpt-5.5"], ["gpt-5.5"], ["automaticai/personal/*"],
            ["automaticai/personal/"], ["automaticai//gpt-5.5"],
            ["automaticai/openrouter/vendor//model"],
            ["automaticai/personal/../model"], ["automaticai/openrouter/vendor/.."],
            ["automaticai/personal/gpt 5.5"], ["automaticai/personal/gpt,5.5"],
        ]
        for routes in invalid:
            with self.subTest(routes=routes):
                env = allowance_envelope(**{"data.applies_to": routes})
                with self.assertRaises(EnvelopeInvalid):
                    validate_envelope(env)

    def test_allowance_limit_specific_ordering_keys_validate(self) -> None:
        for window in ("five_hour", "daily", "weekly", "monthly", "custom"):
            for limit_id in ("unified", "TOKENS_LIMIT", "tool-calls.v1", "x" * 128):
                with self.subTest(window=window, limit_id=limit_id):
                    validate_envelope(allowance_envelope(**{
                        "data.window": window,
                        "data.limit_id": limit_id,
                        "ordering_key": f"allowance:anthropic:claude-personal:{window}:{limit_id}",
                    }))

    def test_allowance_ordering_key_accepts_maximum_components(self) -> None:
        provider, account_id, limit_id = "p" * 64, "a" * 200, "l" * 128
        validate_envelope(allowance_envelope(**{
            "data.provider": provider,
            "data.account_id": account_id,
            "data.limit_id": limit_id,
            "ordering_key": f"allowance:{provider}:{account_id}:five_hour:{limit_id}",
        }))

    def test_allowance_rejects_invalid_ordering_key_suffix(self) -> None:
        for suffix in ("", "x" * 129, "tool calls", "tool/calls", "limit:extra", "é", "*"):
            with self.subTest(suffix=suffix):
                env = allowance_envelope(
                    ordering_key=f"allowance:anthropic:claude-personal:weekly:{suffix}"
                )
                with self.assertRaises(EnvelopeInvalid):
                    validate_envelope(env)

    def test_allowance_rejects_unknown_window_and_unrequested_fields(self) -> None:
        for field, value in (("window", "hourly"), ("collect_status", "ok")):
            with self.subTest(field=field):
                env = allowance_envelope(**{f"data.{field}": value})
                with self.assertRaises(EnvelopeInvalid):
                    validate_envelope(env)

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
