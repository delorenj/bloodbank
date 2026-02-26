"""
Command lifecycle event publisher.

Publishes ack, result, and error events back onto the Bloodbank exchange
with the correct routing keys:
  command.{agent}.{action}.ack
  command.{agent}.{action}.result
  command.{agent}.{action}.error
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import aio_pika
import orjson

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _base_envelope(
    event_type: str,
    payload: dict[str, Any],
    correlation_id: Optional[str] = None,
    causation_id: Optional[str] = None,
    source_app: str = "command-adapter",
) -> dict[str, Any]:
    """Build a base event envelope matching Holyfields base_event.v1 schema."""
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "timestamp": _now_iso(),
        "version": "1.0",
        "source": {
            "host": os.uname().nodename,
            "type": "service",
            "app": source_app,
        },
        "producer": f"service:{source_app}",  # GOD-14 compliance
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "causation_id": causation_id,
        "payload": payload,
    }


class CommandEventPublisher:
    """Publishes command lifecycle events to the Bloodbank exchange."""

    def __init__(self, exchange: aio_pika.Exchange):
        self.exchange = exchange

    async def _publish(self, routing_key: str, envelope: dict[str, Any]) -> None:
        body = orjson.dumps(envelope)
        msg = aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        await self.exchange.publish(msg, routing_key=routing_key)
        logger.info(f"Published {routing_key}: command_id={envelope['payload'].get('command_id', '?')}")

    async def publish_ack(
        self,
        *,
        command_id: str,
        target_agent: str,
        action: str,
        fsm_version: int,
        estimated_duration_ms: Optional[int] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> None:
        """Publish command.{agent}.{action}.ack"""
        payload = {
            "command_id": command_id,
            "target_agent": target_agent,
            "action": action,
            "fsm_version": fsm_version,
            "estimated_duration_ms": estimated_duration_ms,
        }
        envelope = _base_envelope(
            "command.ack", payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        routing_key = f"command.{target_agent}.{action}.ack"
        await self._publish(routing_key, envelope)

    async def publish_result(
        self,
        *,
        command_id: str,
        target_agent: str,
        action: str,
        outcome: str,  # success | partial | skipped
        fsm_version: int,
        duration_ms: Optional[int] = None,
        result_payload: Optional[dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> None:
        """Publish command.{agent}.{action}.result"""
        payload = {
            "command_id": command_id,
            "target_agent": target_agent,
            "action": action,
            "outcome": outcome,
            "fsm_version": fsm_version,
            "duration_ms": duration_ms,
            "result_payload": result_payload,
        }
        envelope = _base_envelope(
            "command.result", payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        routing_key = f"command.{target_agent}.{action}.result"
        await self._publish(routing_key, envelope)

    async def publish_error(
        self,
        *,
        command_id: str,
        target_agent: str,
        action: str,
        error_code: str,
        error_message: str,
        retryable: bool = False,
        retry_after_ms: Optional[int] = None,
        fsm_version: Optional[int] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> None:
        """Publish command.{agent}.{action}.error"""
        payload = {
            "command_id": command_id,
            "target_agent": target_agent,
            "action": action,
            "error_code": error_code,
            "error_message": error_message,
            "retryable": retryable,
            "retry_after_ms": retry_after_ms,
            "fsm_version": fsm_version,
        }
        envelope = _base_envelope(
            "command.error", payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        routing_key = f"command.{target_agent}.{action}.error"
        await self._publish(routing_key, envelope)
