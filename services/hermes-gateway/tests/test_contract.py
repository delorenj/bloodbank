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
        "false-enabled",
        "non-boolean-enabled",
        "wrong-scope",
        "mismatched-target",
        "blank-profile",
    ),
)
def test_registry_route_policy_rejects_ineligible_records(tmp_path, record):
    registry = {"schema_version": 1, "agents": {"fleet-agent": record}}
    (tmp_path / "agents-registry.yaml").write_text(yaml.safe_dump(registry))

    with pytest.raises(RouteInvalid, match="registry-defined but not eligible"):
        _resolver(tmp_path).resolve("fleet-agent")


def test_absent_enabled_key_means_enabled(tmp_path):
    """No key means enabled: a routing record without `enabled` is eligible."""
    registry = {
        "schema_version": 1,
        "agents": {
            "fleet-agent": {
                "profile_name": "operations",
                "bloodbank": {
                    "gateway_scope": "fleet",
                    "target_agent_id": "fleet-agent",
                },
            }
        },
    }
    (tmp_path / "agents-registry.yaml").write_text(yaml.safe_dump(registry))

    assert _resolver(tmp_path).resolve("fleet-agent") == "operations"


@pytest.mark.parametrize(
    "value",
    ["true", "yes", 1, None],
    ids=("string-true", "string-yes", "integer", "explicit-null"),
)
def test_present_non_boolean_enabled_is_invalid_disabled_and_loud(
    tmp_path, caplog, value
):
    registry = {
        "schema_version": 1,
        "agents": {
            "fleet-agent": {
                "profile_name": "operations",
                "bloodbank": {
                    "enabled": value,
                    "gateway_scope": "fleet",
                    "target_agent_id": "fleet-agent",
                },
            }
        },
    }
    (tmp_path / "agents-registry.yaml").write_text(yaml.safe_dump(registry))

    with caplog.at_level("ERROR", logger="bloodbank_hermes_gateway.contract"):
        with pytest.raises(RouteInvalid, match="registry-defined but not eligible"):
            _resolver(tmp_path).resolve("fleet-agent")
    assert any(
        "agents.fleet-agent.bloodbank.enabled must be a strict YAML boolean"
        in record.getMessage()
        for record in caplog.records
    )


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

    # Invocation lifecycle events echo the command's data.context so a
    # downstream consumer can act on a ticket turn statelessly; the turn
    # events do not carry it.
    assert events[1]["data"]["context"] == valid_command["data"]["context"]
    assert events[2]["data"]["context"] == valid_command["data"]["context"]
    assert "context" not in events[0]["data"]
    assert "context" not in events[3]["data"]


def test_invocation_events_omit_context_when_command_has_none(
    valid_command, repo_root
):
    command = json.loads(json.dumps(valid_command))
    command["data"]["context"] = None
    invocation = Invocation.from_envelope(command, "bloodbank-pm")
    events = (
        *started_events(invocation),
        *terminal_events(invocation, outcome="success"),
    )
    for event in events:
        assert "context" not in event["data"]


def test_ticket_context_rides_on_started_completed_and_failed(valid_command, repo_root):
    ticket = {
        "reason": "ticket-grooming",
        "repo": "33GOD",
        "ticket_key": "GOD-1",
        "ticket_id": "c0ffee00-0000-4000-8000-000000000001",
        "board_id": "c0ffee00-0000-4000-8000-000000000002",
        "workspace": "delorenj",
        "title": "Groom me",
        "phase": "grooming",
    }
    command = json.loads(json.dumps(valid_command))
    command["data"]["context"] = ticket
    invocation = Invocation.from_envelope(command, "bloodbank-pm")

    import sys

    sys.path.insert(0, str(repo_root / "services" / "agent-hooks"))
    from core.validate import validate_envelope

    _, started = started_events(invocation)
    completed, _ = terminal_events(invocation, outcome="success")
    failed, _ = terminal_events(invocation, outcome="failure")
    for event in (started, completed, failed):
        validate_envelope(event)
        assert event["data"]["context"] == ticket


def test_decode_command_enforces_the_lane_command_type(valid_command):
    from bloodbank_hermes_gateway.contract import BATCH, INTERACTIVE, LANES

    assert LANES == {"batch": BATCH, "interactive": INTERACTIVE}
    assert INTERACTIVE.subject == "bloodbank.cmd.agent.invocation.invoke"
    assert INTERACTIVE.command_type == "bloodbank.agent.invocation.invoke"

    start = json.dumps(valid_command).encode()
    assert decode_command(start)["type"] == BATCH.command_type
    assert decode_command(start, lane=BATCH)["type"] == BATCH.command_type
    with pytest.raises(CommandInvalid, match="invocation.invoke"):
        decode_command(start, lane=INTERACTIVE)

    invoke = json.loads(json.dumps(valid_command))
    invoke["type"] = INTERACTIVE.command_type
    invoke["dataschema"] = "apicurio://holyfields/bloodbank.agent.invocation.invoke/versions/1"
    invoke["schemaref"] = "bloodbank.agent.invocation.invoke.v1"
    raw = json.dumps(invoke).encode()
    assert decode_command(raw, lane=INTERACTIVE)["type"] == INTERACTIVE.command_type
    with pytest.raises(CommandInvalid, match="invocation.start"):
        decode_command(raw)

    # An invoke envelope carrying the start schema refs is poison.
    mixed = dict(invoke, schemaref="bloodbank.agent.invocation.start.v1")
    with pytest.raises(CommandInvalid, match="schemaref"):
        decode_command(json.dumps(mixed).encode(), lane=INTERACTIVE)


def test_lifecycle_events_carry_the_lane_and_validate(valid_command, repo_root):
    jsonschema = pytest.importorskip("jsonschema")
    for lane_name in ("batch", "interactive"):
        invocation = Invocation.from_envelope(valid_command, "bloodbank-pm", lane_name)
        events = started_events(invocation) + terminal_events(
            invocation, outcome="success"
        )
        for event in events:
            assert event["data"]["lane"] == lane_name
        for event in events:
            if not event["type"].startswith("bloodbank.agent.invocation."):
                continue
            domain, entity, action = event["type"].split(".")[1:]
            schema_path = repo_root / "schemas" / "bloodbank" / domain / f"{entity}.{action}.json"
            schema = json.loads(schema_path.read_text())
            jsonschema.validate(event["data"], schema["properties"]["data"])


def test_interactive_command_schema_matches_the_batch_data_contract(repo_root):
    agent = repo_root / "schemas" / "bloodbank" / "agent"
    start = json.loads((agent / "invocation.start.json").read_text())
    invoke = json.loads((agent / "invocation.invoke.json").read_text())

    def payload(schema):
        data = dict(schema["properties"]["data"])
        data.pop("description")  # names its own type
        return data

    assert payload(start) == payload(invoke)
    assert start["required"] == invoke["required"]
    for key in ("kind", "domain", "delivery", "idempotency_key"):
        assert start["properties"][key] == invoke["properties"][key]
    assert invoke["properties"]["type"]["const"] == "bloodbank.agent.invocation.invoke"
    assert invoke["properties"]["subject"]["const"] == "bloodbank.cmd.agent.invocation.invoke"


def test_turn_timeout_failed_fact_validates_and_cannot_override_owned_fields(
    valid_command, repo_root
):
    import sys

    sys.path.insert(0, str(repo_root / "services" / "agent-hooks"))
    from core.validate import validate_envelope

    invocation = Invocation.from_envelope(valid_command, "bloodbank-pm", "interactive")
    failed, turn = terminal_events(
        invocation,
        outcome="failure",
        failure_code="turn_timeout",
        failure_message="agent turn timed out after 300s on the interactive lane",
        failure_data={
            "elapsed_seconds": 300.2,
            "timeout_seconds": 300.0,
            "timeout_source": "lane",
            "turn_stopped": True,
            "invocation_id": "attacker",
            "lane": "batch",
        },
    )
    for event in (failed, turn):
        validate_envelope(event)
    data = failed["data"]
    assert data["error_code"] == "turn_timeout"
    assert data["timeout_source"] == "lane" and data["turn_stopped"] is True
    assert data["invocation_id"] == valid_command["command_id"]
    assert data["lane"] == "interactive"
    assert turn["data"]["outcome"] == "failed"


def test_progress_event_validates_and_echoes_the_command(valid_command, repo_root):
    import sys

    from bloodbank_hermes_gateway.contract import (
        PROGRESS_TEXT_MAX_CHARS,
        progress_event,
        progress_text,
    )

    sys.path.insert(0, str(repo_root / "services" / "agent-hooks"))
    from core.validate import validate_envelope

    valid_command["data"]["context"] = {"memo_id": "m-1"}
    for lane in ("batch", "interactive"):
        invocation = Invocation.from_envelope(valid_command, "bloodbank-pm", lane)
        text, cut = progress_text("  Checking the board.  ")
        event = progress_event(invocation, seq=3, text=text, truncated=cut, elapsed_seconds=12.345)
        validate_envelope(event)
        assert event["type"] == "bloodbank.agent.invocation.progressed"
        assert event["subject"] == "bloodbank.evt.agent.invocation.progressed"
        assert event["correlationid"] == valid_command["correlationid"]
        assert event["causationid"] == started_events(invocation)[1]["id"]
        assert event["ordering_key"] == f"turn:{invocation.turn_id}"
        d = event["data"]
        assert (d["seq"], d["text"], d["text_truncated"], d["elapsed_seconds"], d["lane"]) == (
            3, "Checking the board.", False, 12.3, lane)
        assert d["command_id"] == valid_command["command_id"]
        assert d["context"] == {"memo_id": "m-1"}

    assert progress_text("   ") == (None, False)
    assert progress_text(None) == (None, False)
    long, cut = progress_text("x" * 2000)
    assert cut is True and len(long) == PROGRESS_TEXT_MAX_CHARS


@pytest.mark.parametrize(
    ("code", "refusal"),
    [
        ("hermes_refused", "gateway_restarting"),
        ("hermes_refused", "gateway.busy.another_turn_running"),
        ("hermes_turn_error", "gateway.errors.generic_failed_with_hint"),
    ],
)
def test_hermes_refusal_failed_fact_validates(valid_command, repo_root, code, refusal):
    import sys

    sys.path.insert(0, str(repo_root / "services" / "agent-hooks"))
    from core.validate import validate_envelope

    for lane in ("batch", "interactive"):
        invocation = Invocation.from_envelope(valid_command, "bloodbank-pm", lane)
        failed, turn = terminal_events(
            invocation,
            outcome="failure",
            failure_code=code,
            failure_message=f"Hermes refused the turn ({refusal})",
            failure_data={"refusal": refusal, "error_code": "attacker"},
        )
        for event in (failed, turn):
            validate_envelope(event)
        assert failed["data"]["error_code"] == code
        assert failed["data"]["refusal"] == refusal
        assert turn["data"]["outcome"] == "failed"
