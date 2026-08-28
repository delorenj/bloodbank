from __future__ import annotations

import json

import pytest
import yaml

from bloodbank_hermes_gateway.contract import (
    CommandInvalid,
    Invocation,
    ProfileResolver,
    RegistryInvalid,
    RouteInvalid,
    decode_command,
    started_events,
    terminal_events,
)


def _resolver(tmp_path, *, target_profiles=None, direct=False, known=None):
    known = known or {"research", "operations", "bloodbank-pm"}
    return ProfileResolver(
        target_profiles=target_profiles,
        fleet_registry=tmp_path / "agents-registry.yaml",
        allow_direct_profile_targets=direct,
        normalize_profile_name=lambda name: name.strip().lower(),
        validate_profile_name=lambda name: None,
        profile_exists=lambda name: name in known,
    )


def test_decode_command_is_strict(valid_command):
    decoded = decode_command(json.dumps(valid_command).encode())
    assert decoded["command_id"] == valid_command["command_id"]

    invalid = dict(valid_command)
    invalid["kind"] = "event"
    with pytest.raises(CommandInvalid, match="kind"):
        decode_command(json.dumps(invalid).encode())

    missing_prompt = json.loads(json.dumps(valid_command))
    missing_prompt["data"]["prompt"] = None
    with pytest.raises(CommandInvalid, match="data.prompt"):
        decode_command(json.dumps(missing_prompt).encode())


def test_profile_routing_precedence_and_fleet_registry(tmp_path):
    registry = {
        "schema_version": 1,
        "agents": {
            "fleet-agent": {
                "profile_name": "operations",
                "bloodbank": {
                    "enabled": True,
                    "gateway_scope": "fleet",
                    "target_agent_id": "fleet-agent",
                },
            },
            "overridden": {
                "profile_name": "operations",
                "bloodbank": {
                    "enabled": True,
                    "gateway_scope": "fleet",
                    "target_agent_id": "overridden",
                },
            },
        },
    }
    (tmp_path / "agents-registry.yaml").write_text(yaml.safe_dump(registry))
    resolver = _resolver(
        tmp_path,
        target_profiles={"explicit": "research", "overridden": "research"},
    )

    assert resolver.resolve("explicit") == "research"
    assert resolver.resolve("fleet-agent") == "operations"
    assert resolver.resolve("overridden") == "research"


def test_active_registry_route_requires_exact_bloodbank_policy(tmp_path):
    registry = {
        "schema_version": 1,
        "agents": {
            "fleet-agent": {
                "profile_name": "operations",
                "bloodbank": {
                    "enabled": True,
                    "gateway_scope": "fleet",
                    "target_agent_id": "fleet-agent",
                },
            }
        },
    }
    (tmp_path / "agents-registry.yaml").write_text(yaml.safe_dump(registry))

    assert _resolver(tmp_path).resolve("fleet-agent") == "operations"


@pytest.mark.parametrize(
    "record",
    [
        {"profile_name": "operations"},
        {
            "profile_name": "operations",
            "telegram": {"enabled": True},
            "systemd": {"active": True},
            "lifecycle": "active",
        },
        {"profile_name": "operations", "bloodbank": None},
        {"profile_name": "operations", "bloodbank": "enabled"},
        {
            "profile_name": "operations",
            "bloodbank": {
                "gateway_scope": "fleet",
                "target_agent_id": "fleet-agent",
            },
        },
        {
            "profile_name": "operations",
            "bloodbank": {
                "enabled": False,
                "gateway_scope": "fleet",
                "target_agent_id": "fleet-agent",
            },
        },
        {
            "profile_name": "operations",
            "bloodbank": {
                "enabled": "true",
                "gateway_scope": "fleet",
                "target_agent_id": "fleet-agent",
            },
        },
        {
            "profile_name": "operations",
            "bloodbank": {
                "enabled": True,
                "gateway_scope": "profile",
                "target_agent_id": "fleet-agent",
            },
        },
        {
            "profile_name": "operations",
            "bloodbank": {
                "enabled": True,
                "gateway_scope": "fleet",
                "target_agent_id": "other-agent",
            },
        },
        {
            "profile_name": " ",
            "bloodbank": {
                "enabled": True,
                "gateway_scope": "fleet",
                "target_agent_id": "fleet-agent",
            },
        },
    ],
    ids=(
        "missing-bloodbank",
        "unrelated-runtime-signals",
        "null-bloodbank",
        "malformed-bloodbank",
        "missing-enabled",
        "false-enabled",
        "non-boolean-enabled",
        "wrong-scope",
        "mismatched-target",
        "blank-profile",
    ),
)
def test_registry_route_policy_is_strict_default_deny(tmp_path, record):
    registry = {"schema_version": 1, "agents": {"fleet-agent": record}}
    (tmp_path / "agents-registry.yaml").write_text(yaml.safe_dump(registry))

    with pytest.raises(RouteInvalid, match="registry-defined but not eligible"):
        _resolver(tmp_path).resolve("fleet-agent")


def test_static_target_profile_is_an_explicit_registry_independent_override(tmp_path):
    resolver = _resolver(tmp_path, target_profiles={"fleet-agent": "research"})

    assert resolver.resolve("fleet-agent") == "research"


def test_direct_profile_fallback_cannot_bypass_disabled_registry_record(tmp_path):
    registry = {
        "schema_version": 1,
        "agents": {
            "research": {
                "profile_name": "research",
                "bloodbank": {
                    "enabled": False,
                    "gateway_scope": "fleet",
                    "target_agent_id": "research",
                },
            }
        },
    }
    (tmp_path / "agents-registry.yaml").write_text(yaml.safe_dump(registry))

    with pytest.raises(RouteInvalid, match="registry-defined but not eligible"):
        _resolver(tmp_path, direct=True).resolve("research")


@pytest.mark.parametrize(
    "registry_agent_id",
    [" bloodbank-pm ", "Bloodbank-pm"],
    ids=("padded", "case-variant"),
)
def test_noncanonical_registry_id_invalidates_before_direct_fallback(
    tmp_path, registry_agent_id
):
    registry = {
        "schema_version": 1,
        "agents": {
            registry_agent_id: {
                "profile_name": "bloodbank-pm",
                "bloodbank": {
                    "enabled": False,
                    "gateway_scope": "fleet",
                    "target_agent_id": registry_agent_id,
                },
            }
        },
    }
    (tmp_path / "agents-registry.yaml").write_text(yaml.safe_dump(registry))

    with pytest.raises(RegistryInvalid, match="canonical lowercase slugs"):
        _resolver(tmp_path, direct=True).resolve("bloodbank-pm")


def test_unknown_and_direct_profile_routes_fail_closed(tmp_path):
    (tmp_path / "agents-registry.yaml").write_text(
        "schema_version: 1\nagents: {}\n", encoding="utf-8"
    )
    resolver = _resolver(tmp_path)
    with pytest.raises(RouteInvalid, match="no configured profile route"):
        resolver.resolve("research")

    direct = _resolver(tmp_path, direct=True)
    assert direct.resolve("research") == "research"
    with pytest.raises(RouteInvalid):
        direct.resolve("Research")
    with pytest.raises(RouteInvalid):
        direct.resolve("missing-profile")


def test_missing_or_invalid_registry_is_transient(tmp_path):
    resolver = _resolver(tmp_path)
    with pytest.raises(RegistryInvalid, match="missing"):
        resolver.resolve("research")

    (tmp_path / "agents-registry.yaml").write_text(
        "agents: [invalid\n", encoding="utf-8"
    )
    with pytest.raises(RegistryInvalid, match="unreadable or invalid"):
        resolver.resolve("research")


@pytest.mark.parametrize(
    "registry_text,target,known,error",
    [
        ("", "research", {"research"}, "root must be a mapping"),
        (
            "schema_version: 1\n",
            "research",
            {"research"},
            "agents must be a mapping",
        ),
        (
            "schema_version: 2\nagents: {}\n",
            "research",
            {"research"},
            "schema_version must be exactly 1",
        ),
        (
            "schema_version: 1\nagents:\n  123: {}\n",
            "123",
            {"123"},
            "identifiers must be non-empty strings",
        ),
        (
            "schema_version: 1\nagents:\n  research: []\n",
            "research",
            {"research"},
            "metadata for 'research' must be a mapping",
        ),
    ],
    ids=(
        "empty-file",
        "missing-agents",
        "unsupported-version",
        "numeric-key-direct-bypass",
        "malformed-metadata",
    ),
)
def test_structurally_invalid_registry_is_transient_before_direct_fallback(
    tmp_path, registry_text, target, known, error
):
    (tmp_path / "agents-registry.yaml").write_text(registry_text, encoding="utf-8")
    resolver = _resolver(tmp_path, direct=True, known=known)

    with pytest.raises(RegistryInvalid, match=error):
        resolver.resolve(target)


@pytest.mark.parametrize(
    "outcome,invocation_type,turn_outcome",
    [
        ("success", "bloodbank.agent.invocation.completed", "completed"),
        ("failure", "bloodbank.agent.invocation.failed", "failed"),
        ("cancelled", "bloodbank.agent.invocation.failed", "canceled"),
    ],
)
def test_lifecycle_events_use_existing_schema_contracts(
    valid_command, repo_root, outcome, invocation_type, turn_outcome
):
    invocation = Invocation.from_envelope(valid_command, "bloodbank-pm")
    events = (
        *started_events(invocation),
        *terminal_events(invocation, outcome=outcome),
    )

    import sys

    sys.path.insert(0, str(repo_root / "services" / "agent-hooks"))
    from core.validate import validate_envelope

    for event in events:
        validate_envelope(event)
        assert event["correlationid"] == valid_command["correlationid"]
        assert event["data"]["command_id"] == valid_command["command_id"]
        assert event["data"]["idempotency_key"] == valid_command["idempotency_key"]

    assert events[2]["type"] == invocation_type
    assert events[3]["data"]["outcome"] == turn_outcome
