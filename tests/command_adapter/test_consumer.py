"""
Tests for CommandAdapter message parsing, guard handling, and lifecycle flow.

These are unit tests that mock RabbitMQ, Redis, and OpenClaw hooks.
Integration tests require real Redis (Lua scripts) + real RabbitMQ.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from command_adapter.consumer import _parse_command_routing_key


# ---------------------------------------------------------------------------
# Routing key parser tests
# ---------------------------------------------------------------------------


class TestParseCommandRoutingKey:
    def test_valid_envelope(self):
        assert _parse_command_routing_key("command.lenoon.run_drift_check") == ("lenoon", "run_drift_check")

    def test_valid_with_underscores(self):
        assert _parse_command_routing_key("command.momothecat.assign_ticket") == ("momothecat", "assign_ticket")

    def test_ack_is_skipped(self):
        assert _parse_command_routing_key("command.lenoon.run_drift_check.ack") is None

    def test_result_is_skipped(self):
        assert _parse_command_routing_key("command.lenoon.run_drift_check.result") is None

    def test_error_is_skipped(self):
        assert _parse_command_routing_key("command.lenoon.run_drift_check.error") is None

    def test_not_command(self):
        assert _parse_command_routing_key("agent.lenoon.status") is None

    def test_too_short(self):
        assert _parse_command_routing_key("command.lenoon") is None

    def test_empty(self):
        assert _parse_command_routing_key("") is None

    def test_unknown_suffix_treated_as_envelope(self):
        # 4-part key with unknown suffix: falls through len>=3 check,
        # extracts agent + action (extra segments ignored). This is correct —
        # only ack/result/error suffixes are explicitly filtered out.
        result = _parse_command_routing_key("command.lenoon.check.unknown")
        assert result == ("lenoon", "check")


# ---------------------------------------------------------------------------
# Publisher tests
# ---------------------------------------------------------------------------


class TestPublisher:
    @pytest.mark.asyncio
    async def test_publish_ack_routing_key(self):
        from command_adapter.publisher import CommandEventPublisher

        mock_exchange = AsyncMock()
        publisher = CommandEventPublisher(mock_exchange)

        await publisher.publish_ack(
            command_id="abc-123",
            target_agent="lenoon",
            action="run_drift_check",
            fsm_version=2,
            correlation_id="corr-1",
            causation_id="cause-1",
        )

        mock_exchange.publish.assert_called_once()
        call_args = mock_exchange.publish.call_args
        assert call_args.kwargs["routing_key"] == "command.lenoon.run_drift_check.ack"

    @pytest.mark.asyncio
    async def test_publish_result_routing_key(self):
        from command_adapter.publisher import CommandEventPublisher

        mock_exchange = AsyncMock()
        publisher = CommandEventPublisher(mock_exchange)

        await publisher.publish_result(
            command_id="abc-123",
            target_agent="grolf",
            action="assign_ticket",
            outcome="success",
            fsm_version=4,
            duration_ms=1500,
        )

        call_args = mock_exchange.publish.call_args
        assert call_args.kwargs["routing_key"] == "command.grolf.assign_ticket.result"

    @pytest.mark.asyncio
    async def test_publish_error_routing_key(self):
        from command_adapter.publisher import CommandEventPublisher

        mock_exchange = AsyncMock()
        publisher = CommandEventPublisher(mock_exchange)

        await publisher.publish_error(
            command_id="abc-123",
            target_agent="cack",
            action="delegate_task",
            error_code="timeout",
            error_message="Hook timed out",
            retryable=True,
        )

        call_args = mock_exchange.publish.call_args
        assert call_args.kwargs["routing_key"] == "command.cack.delegate_task.error"


# ---------------------------------------------------------------------------
# OpenClaw hook dispatcher tests
# ---------------------------------------------------------------------------


class TestOpenClawHook:
    @pytest.mark.asyncio
    async def test_dispatch_success(self):
        from command_adapter.openclaw_hook import OpenClawHookDispatcher

        dispatcher = OpenClawHookDispatcher(
            hook_url="http://localhost:18789/hooks/agent",
            hook_token="test-token",
        )

        with patch("command_adapter.openclaw_hook.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 202
            mock_resp.json.return_value = {"status": "accepted"}
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await dispatcher.dispatch(
                target_agent="lenoon",
                action="run_drift_check",
                command_id="test-id",
                issued_by="grolf",
            )

            assert result.success is True
            assert result.status_code == 202

    @pytest.mark.asyncio
    async def test_dispatch_timeout(self):
        import httpx
        from command_adapter.openclaw_hook import OpenClawHookDispatcher

        dispatcher = OpenClawHookDispatcher(
            hook_url="http://localhost:18789/hooks/agent",
            hook_token="test-token",
            timeout_seconds=1.0,
        )

        with patch("command_adapter.openclaw_hook.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
            mock_client_cls.return_value = mock_client

            result = await dispatcher.dispatch(
                target_agent="lenoon",
                action="run_drift_check",
                command_id="test-id",
                issued_by="grolf",
            )

            assert result.success is False
            assert "Timeout" in result.error


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestConfig:
    def test_agent_roster_parsing(self):
        from command_adapter.config import AdapterConfig

        config = AdapterConfig(agent_roster="lenoon,grolf,cack")
        assert config.agents == ["lenoon", "grolf", "cack"]

    def test_wildcard_roster(self):
        from command_adapter.config import AdapterConfig

        config = AdapterConfig(agent_roster="*")
        assert config.agents == []
