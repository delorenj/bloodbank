"""
Agent Command Adapter — FastStream consumer (GOD-3 / AGENT-CTRL-1).

Lifecycle per command message:
  1. Deserialize CommandEnvelope from routing key command.{agent}.{action}
  2. Run guards: TTL → idempotency → FSM state
  3. FSM: idle → acknowledging, publish ack
  4. FSM: acknowledging → working
  5. Dispatch to OpenClaw hooks
  6. On success: publish result, FSM: working → idle
  7. On failure: publish error, FSM: working → error

See: docs/architecture/COMMAND-SYSTEM-RFC.md §5
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import aio_pika
import orjson

from command_fsm.manager import FSMManager, CommandGuardResult
from command_fsm.states import FSMState
from .config import AdapterConfig
from .dispatcher import DispatcherRegistry, DispatchResult, build_registry_from_env
from .openclaw_hook import OpenClawHookDispatcher
from .http_dispatcher import http_dispatcher_factory
from .publisher import CommandEventPublisher

logger = logging.getLogger(__name__)


def _parse_command_routing_key(routing_key: str) -> tuple[str, str] | None:
    """Parse command.{agent}.{action} → (agent, action) or None."""
    parts = routing_key.split(".")
    if len(parts) >= 3 and parts[0] == "command":
        # Skip ack/result/error suffixes — those are lifecycle events, not commands
        if len(parts) == 4 and parts[3] in ("ack", "result", "error"):
            return None
        return (parts[1], parts[2])
    return None


class CommandAdapter:
    """
    Consumes commands from Bloodbank, runs FSM lifecycle, dispatches via pluggable registry.
    """

    def __init__(self, config: AdapterConfig | None = None):
        self.config = config or AdapterConfig()
        self.fsm: FSMManager | None = None
        self.publisher: CommandEventPublisher | None = None
        self.registry: DispatcherRegistry | None = None
        self._conn: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None

    async def start(self) -> None:
        """Initialize connections and start consuming."""
        logger.info("Starting command adapter...")

        # FSM manager
        self.fsm = FSMManager(redis_url=self.config.redis_url)

        openclaw_dispatcher = OpenClawHookDispatcher(
            hook_url=self.config.openclaw_hook_url,
            hook_token=self.config.openclaw_hook_token,
            timeout_seconds=self.config.hook_timeout_seconds,
        )
        self.registry = build_registry_from_env(
            openclaw_dispatcher=openclaw_dispatcher,
            http_dispatcher_factory=http_dispatcher_factory,
        )

        # RabbitMQ connection
        self._conn = await aio_pika.connect_robust(
            self.config.rabbitmq_url, heartbeat=30
        )
        self._channel = await self._conn.channel()
        await self._channel.set_qos(prefetch_count=1)  # Process one command at a time

        # Declare exchange
        exchange = await self._channel.declare_exchange(
            self.config.exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        self.publisher = CommandEventPublisher(exchange)

        # Declare queue and bind to command routing keys
        queue_name = "agent.commands"  # Single queue for all agents
        queue = await self._channel.declare_queue(queue_name, durable=True)

        agents = self.config.agents
        if not agents:
            # Subscribe to all commands
            await queue.bind(exchange, routing_key="command.#")
            logger.info("Bound to command.# (all agents)")
        else:
            for agent in agents:
                rk = f"command.{agent}.#"
                await queue.bind(exchange, routing_key=rk)
                logger.info(f"Bound to {rk}")

        # Start consuming
        await queue.consume(self._on_message)
        logger.info(
            f"Command adapter started — {len(agents) if agents else 'all'} agents, "
            f"queue={queue_name}"
        )

    async def _on_message(self, message: aio_pika.IncomingMessage) -> None:
        """Process a single command message."""
        async with message.process(requeue=True):
            routing_key = message.routing_key or ""

            parsed = _parse_command_routing_key(routing_key)
            if not parsed:
                # Not a command envelope — skip (ack/result/error messages)
                return

            agent_name, action = parsed

            try:
                body = orjson.loads(message.body)
            except Exception:
                logger.error(f"Failed to parse message body on {routing_key}")
                return

            payload = body.get("payload", {})
            command_id = payload.get("command_id", body.get("event_id", ""))
            issued_by = payload.get("issued_by", "unknown")
            priority = payload.get("priority", "normal")
            ttl_ms = payload.get("ttl_ms", 30000)
            idempotency_key = payload.get("idempotency_key")
            command_payload = payload.get("command_payload", {})
            correlation_id = body.get("correlation_id")
            causation_id = body.get("event_id")  # The command event_id is the causation

            # Parse issued_at from envelope timestamp
            try:
                issued_at = datetime.fromisoformat(body.get("timestamp", ""))
            except (ValueError, TypeError):
                issued_at = datetime.now(timezone.utc)

            logger.info(
                f"Command received: agent={agent_name} action={action} "
                f"id={command_id} from={issued_by} priority={priority}"
            )

            await self._process_command(
                agent_name=agent_name,
                action=action,
                command_id=command_id,
                issued_by=issued_by,
                issued_at=issued_at,
                priority=priority,
                ttl_ms=ttl_ms,
                idempotency_key=idempotency_key,
                command_payload=command_payload,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )

    async def _process_command(
        self,
        *,
        agent_name: str,
        action: str,
        command_id: str,
        issued_by: str,
        issued_at: datetime,
        priority: str,
        ttl_ms: int,
        idempotency_key: str | None,
        command_payload: dict[str, Any],
        correlation_id: str | None,
        causation_id: str | None,
    ) -> None:
        """Full command processing lifecycle with FSM guards."""
        start_time = time.monotonic()

        # ── Step 1: Guard checks + FSM transition to acknowledging ──
        try:
            command_uuid = UUID(command_id)
        except (ValueError, TypeError):
            command_uuid = UUID(int=0)

        guard_result, state = self.fsm.accept_command(
            agent_name=agent_name,
            command_id=command_uuid,
            issued_at=issued_at,
            ttl_ms=ttl_ms,
            idempotency_key=idempotency_key,
        )

        if guard_result == CommandGuardResult.EXPIRED:
            await self.publisher.publish_error(
                command_id=command_id,
                target_agent=agent_name,
                action=action,
                error_code="ttl_expired",
                error_message=f"Command expired (TTL {ttl_ms}ms)",
                retryable=False,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return

        if guard_result == CommandGuardResult.DUPLICATE:
            await self.publisher.publish_result(
                command_id=command_id,
                target_agent=agent_name,
                action=action,
                outcome="skipped",
                fsm_version=state.version if state else 0,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return

        if guard_result == CommandGuardResult.INVALID_STATE:
            await self.publisher.publish_error(
                command_id=command_id,
                target_agent=agent_name,
                action=action,
                error_code="invalid_state",
                error_message=f"Agent {agent_name} not idle (current: {state.state.value if state else '?'})",
                retryable=True,
                retry_after_ms=5000,
                fsm_version=state.version if state else None,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return

        if guard_result == CommandGuardResult.PAUSED:
            await self.publisher.publish_error(
                command_id=command_id,
                target_agent=agent_name,
                action=action,
                error_code="rejected",
                error_message=f"Agent {agent_name} is paused",
                retryable=True,
                retry_after_ms=10000,
                fsm_version=state.version if state else None,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return

        if guard_result == CommandGuardResult.VERSION_CONFLICT:
            await self.publisher.publish_error(
                command_id=command_id,
                target_agent=agent_name,
                action=action,
                error_code="rejected",
                error_message="FSM version conflict after max retries",
                retryable=True,
                retry_after_ms=1000,
                fsm_version=state.version if state else None,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return

        # ── Step 2: Publish ack ──
        await self.publisher.publish_ack(
            command_id=command_id,
            target_agent=agent_name,
            action=action,
            fsm_version=state.version,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

        # ── Step 3: FSM acknowledging → working ──
        ok, state = self.fsm.mark_working(agent_name)
        if not ok:
            logger.error(f"FSM transition to working failed for {agent_name}")

        dispatcher = self.registry.get_dispatcher(agent_name) if self.registry else None
        if not dispatcher:
            logger.error(f"No dispatcher found for agent {agent_name}")
            await self.publisher.publish_error(
                command_id=command_id,
                target_agent=agent_name,
                action=action,
                error_code="not_configured",
                error_message=f"No dispatcher configured for agent {agent_name}",
                retryable=False,
                fsm_version=state.version if state else 0,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return

        dispatch_result: DispatchResult = await dispatcher.dispatch(
            target_agent=agent_name,
            action=action,
            command_id=command_id,
            issued_by=issued_by,
            priority=priority,
            command_payload=command_payload,
        )

        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        if dispatch_result.success:
            ok, state = self.fsm.mark_completed(agent_name)
            await self.publisher.publish_result(
                command_id=command_id,
                target_agent=agent_name,
                action=action,
                outcome="success",
                fsm_version=state.version if state else 0,
                duration_ms=elapsed_ms,
                result_payload=dispatch_result.response_body,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
        else:
            ok, state = self.fsm.mark_failed(agent_name)

            if dispatch_result.status_code == 0 and "Timeout" in (
                dispatch_result.error or ""
            ):
                error_code = "timeout"
            elif dispatch_result.status_code == 0:
                error_code = "execution_failed"
            elif dispatch_result.status_code == 404:
                error_code = "not_implemented"
            elif dispatch_result.status_code == 429:
                error_code = "rate_limited"
            else:
                error_code = "execution_failed"

            await self.publisher.publish_error(
                command_id=command_id,
                target_agent=agent_name,
                action=action,
                error_code=error_code,
                error_message=dispatch_result.error
                or f"HTTP {dispatch_result.status_code}",
                retryable=error_code in ("timeout", "rate_limited", "execution_failed"),
                retry_after_ms=5000 if error_code == "rate_limited" else None,
                fsm_version=state.version if state else None,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )

    async def close(self) -> None:
        """Shut down connections."""
        if self._conn:
            await self._conn.close()
        logger.info("Command adapter stopped")
