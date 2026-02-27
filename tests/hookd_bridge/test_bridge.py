"""
Tests for hookd compatibility bridge.
"""
import pytest

from hookd_bridge.bridge import (
    parse_hook_text,
    extract_agent_from_session_key,
    build_command_envelope,
    is_empty_noop,
    extract_hook_text,
)


class TestParseHookText:
    def test_structured_command(self):
        text = "[Command] action=run_drift_check id=abc-123 from=grolf priority=high"
        action, issued_by, priority, payload = parse_hook_text(text)
        assert action == "run_drift_check"
        assert issued_by == "grolf"
        assert priority == "high"

    def test_structured_with_json_payload(self):
        text = '[Command] action=assign_ticket from=cack\n{"ticket_id": "GOD-5", "component": "holocene"}'
        action, issued_by, priority, payload = parse_hook_text(text)
        assert action == "assign_ticket"
        assert issued_by == "cack"
        assert priority == "normal"
        assert payload["ticket_id"] == "GOD-5"

    def test_unstructured_text(self):
        text = "Check the RabbitMQ logs for auth failures"
        action, issued_by, priority, payload = parse_hook_text(text)
        assert action == "hook_dispatch"
        assert issued_by == "hookd-bridge"
        assert payload["raw_text"] == text

    def test_partial_command(self):
        text = "[Command] action=healthcheck"
        action, issued_by, priority, payload = parse_hook_text(text)
        assert action == "healthcheck"
        assert issued_by == "hookd-bridge"
        assert priority == "normal"

    def test_git_maintenance_structured_alias(self):
        text = "[Command] action=git-maintenance"
        action, issued_by, priority, payload = parse_hook_text(text)
        assert action == "run_git_maintenance"
        assert issued_by == "hookd-bridge"
        assert priority == "normal"

    def test_git_maintenance_shorthand(self):
        text = "git maintenance"
        action, issued_by, priority, payload = parse_hook_text(text)
        assert action == "run_git_maintenance"
        assert issued_by == "hookd-bridge"

    def test_empty_text(self):
        action, issued_by, priority, payload = parse_hook_text("")
        assert action == "hook_dispatch"


class TestNoopFilter:
    def test_empty_hook_dispatch_is_noop(self):
        assert is_empty_noop("", "hook_dispatch", {"raw_text": ""}) is True

    def test_non_empty_text_is_not_noop(self):
        assert is_empty_noop("ping", "hook_dispatch", {"raw_text": "ping"}) is False

    def test_non_dispatch_action_is_not_noop(self):
        assert is_empty_noop("", "run_git_maintenance", {"raw_text": ""}) is False


class TestHookTextAlias:
    def test_prefers_text(self):
        body = {"text": "primary", "message": "legacy"}
        assert extract_hook_text(body) == "primary"

    def test_falls_back_to_legacy_message(self):
        body = {"text": "", "message": "legacy payload"}
        assert extract_hook_text(body) == "legacy payload"

    def test_handles_non_string_message(self):
        body = {"message": 12345}
        assert extract_hook_text(body) == "12345"


class TestExtractAgent:
    def test_standard_session_key(self):
        assert extract_agent_from_session_key("agent:lenoon:main") == "lenoon"

    def test_nested_session_key(self):
        assert extract_agent_from_session_key("agent:grolf:telegram:direct") == "grolf"

    def test_invalid_key(self):
        assert extract_agent_from_session_key("not-a-session-key") is None

    def test_empty(self):
        assert extract_agent_from_session_key("") is None


class TestBuildEnvelope:
    def test_basic_envelope(self):
        rk, env = build_command_envelope(
            target_agent="lenoon",
            action="run_drift_check",
            issued_by="grolf",
        )
        assert rk == "command.lenoon.run_drift_check"
        assert env["event_type"] == "command.envelope"
        assert env["payload"]["target_agent"] == "lenoon"
        assert env["payload"]["action"] == "run_drift_check"
        assert env["payload"]["issued_by"] == "grolf"
        assert env["version"] == "1.0.0"
        assert env["source"]["app"] == "hookd-bridge"
        assert env["source"]["type"] == "webhook"
        assert env["correlation_ids"] == [env["correlation_id"]]

    def test_with_payload(self):
        rk, env = build_command_envelope(
            target_agent="cack",
            action="delegate_task",
            command_payload={"task": "fix auth spam"},
        )
        assert env["payload"]["command_payload"]["task"] == "fix auth spam"

    def test_routing_key_format(self):
        rk, _ = build_command_envelope(
            target_agent="rererere",
            action="build_feature",
        )
        assert rk == "command.rererere.build_feature"

    def test_uuid_fields(self):
        _, env = build_command_envelope(target_agent="yi", action="test")
        assert len(env["event_id"]) == 36  # UUID format
        assert len(env["payload"]["command_id"]) == 36
        assert len(env["correlation_id"]) == 36
