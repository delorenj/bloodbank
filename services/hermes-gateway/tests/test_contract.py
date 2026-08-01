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
            "fleet-agent": {"profile_name": "operations"},
            "overridden": {"profile_name": "operations"},
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

    (tmp_path / "agents-registry.yaml").write_text("agents: [invalid\n", encoding="utf-8")
    with pytest.raises(RegistryInvalid, match="unreadable or invalid"):
        resolver.resolve("research")


@pytest.mark.parametrize("outcome,invocation_type,turn_outcome", [
    ("success", "bloodbank.v1.agent.invocation.completed", "completed"),
    ("failure", "bloodbank.v1.agent.invocation.failed", "failed"),
    ("cancelled", "bloodbank.v1.agent.invocation.failed", "canceled"),
])
def test_lifecycle_events_use_existing_schema_contracts(
    valid_command, repo_root, outcome, invocation_type, turn_outcome
):
    invocation = Invocation.from_envelope(valid_command, "bloodbank-pm")
    events = (*started_events(invocation), *terminal_events(invocation, outcome=outcome))

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
