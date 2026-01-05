"""
RabbitMQ publisher with built-in correlation tracking.

This module provides the Publisher class for connecting to and publishing
messages to RabbitMQ with automatic correlation tracking via Redis.
"""

import asyncio
from urllib.parse import urlparse, urlunparse
from typing import Any, Dict, Optional, List
from uuid import UUID
import orjson
import aio_pika
import logging

from event_producers.config import settings
from event_producers.correlation_tracker import CorrelationTracker

logger = logging.getLogger(__name__)


def _redacted_url(url: str) -> str:
    """Redact credentials from URL for safe logging."""
    parsed = urlparse(url)
    if not parsed.netloc:
        return url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    new_netloc = host + port
    redacted = parsed._replace(netloc=new_netloc)
    return urlunparse(redacted)


class Publisher:
    """
    RabbitMQ publisher with optional correlation tracking.

    Features:
    - Automatic correlation tracking via Redis (optional)
    - Deterministic event ID generation for idempotency
    - Persistent message publishing
    - Async/await support
    - Graceful degradation if Redis is unavailable

    Usage:
        # Basic usage (no correlation tracking)
        publisher = Publisher()
        await publisher.start()

        # With correlation tracking
        publisher = Publisher(enable_correlation_tracking=True)
        await publisher.start()

        # Generate idempotent event ID
        event_id = publisher.generate_event_id(
            "fireflies.transcript.upload",
            meeting_id="abc123"
        )

        # Publish with automatic correlation tracking
        await publisher.publish(
            routing_key="fireflies.transcript.ready",
            body=envelope.model_dump(mode="json"),
            event_id=event_id,
            parent_event_ids=[previous_event_id]
        )
    """

    def __init__(
        self,
        enable_correlation_tracking: bool = False,
        redis_host: Optional[str] = None,
        redis_port: Optional[int] = None,
        redis_password: Optional[str] = None,
    ):
        """
        Initialize publisher.

        Args:
            enable_correlation_tracking: Whether to use Redis correlation tracking
            redis_host: Redis hostname (default from settings)
            redis_port: Redis port (default from settings)
            redis_password: Redis password (default from settings)
        """
        self._conn = None
        self._channel = None
        self._exchange = None
        self._lock = asyncio.Lock()
        self._started = False

        # Correlation tracking (optional)
        self.enable_correlation_tracking = enable_correlation_tracking
        if enable_correlation_tracking:
            self.tracker = CorrelationTracker(
                redis_host=redis_host or getattr(settings, "redis_host", "localhost"),
                redis_port=redis_port or getattr(settings, "redis_port", 6379),
                redis_password=redis_password
                or getattr(settings, "redis_password", None),
            )
        else:
            self.tracker = None

    async def start(self):
        """Connect to RabbitMQ and optionally Redis."""
        if self._started:
            return

        async with self._lock:
            if self._started:
                return

            # Connect to RabbitMQ
            if not settings.rabbit_url:
                raise RuntimeError(
                    "RABBIT_URL is not configured; set the environment variable."
                )

            try:
                self._conn = await asyncio.wait_for(
                    aio_pika.connect_robust(settings.rabbit_url), timeout=10
                )
                self._channel = await self._conn.channel(publisher_confirms=True)
                self._exchange = await self._channel.declare_exchange(
                    settings.exchange_name, aio_pika.ExchangeType.TOPIC, durable=True
                )
                logger.info(
                    f"Publisher: Connected to RabbitMQ exchange '{settings.exchange_name}'"
                )
            except Exception as exc:
                safe_url = _redacted_url(settings.rabbit_url)
                raise RuntimeError(
                    f"Failed to connect to RabbitMQ at '{safe_url}': {exc}"
                ) from exc

            # Connect to Redis if correlation tracking enabled
            if self.enable_correlation_tracking and self.tracker:
                try:
                    await self.tracker.start()
                    logger.info("Publisher: Correlation tracking enabled")
                except Exception as e:
                    logger.warning(
                        f"Publisher: Correlation tracking disabled due to error: {e}"
                    )
                    # Don't fail startup - correlation tracking is optional
                    self.enable_correlation_tracking = False

            self._started = True

    async def publish(
        self,
        routing_key: str,
        body: Dict[str, Any],
        message_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        event_id: Optional[UUID] = None,
        parent_event_ids: Optional[List[UUID]] = None,
        correlation_metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        Publish a message to the exchange with optional correlation tracking.

        Args:
            routing_key: RabbitMQ routing key (should match event_type)
            body: Message body (dict, will be JSON serialized)
            message_id: DEPRECATED - use event_id instead
            correlation_id: DEPRECATED - use parent_event_ids instead
            event_id: Event UUID (extracted from body if not provided)
            parent_event_ids: List of parent event UUIDs for correlation
            correlation_metadata: Optional metadata about the correlation

        Example:
            # Simple publish
            await publisher.publish(
                routing_key="fireflies.transcript.ready",
                body=envelope.model_dump(mode="json")
            )

            # With correlation tracking
            await publisher.publish(
                routing_key="fireflies.transcript.processed",
                body=envelope.model_dump(mode="json"),
                parent_event_ids=[ready_event_id]
            )
        """
        if not self._exchange:
            await self.start()

        # Extract event_id from body if not provided
        if event_id is None:
            event_id_str = body.get("event_id")
            if event_id_str:
                event_id = (
                    UUID(event_id_str)
                    if isinstance(event_id_str, str)
                    else event_id_str
                )

        # Track correlation if enabled
        if (
            self.enable_correlation_tracking
            and self.tracker
            and event_id
            and parent_event_ids
        ):
            try:
                await asyncio.wait_for(
                    self.tracker.add_correlation(
                        child_event_id=event_id,
                        parent_event_ids=parent_event_ids,
                        metadata=correlation_metadata,
                    ),
                    timeout=1.0,  # Don't block publishing for correlation tracking
                )
            except asyncio.TimeoutError:
                logger.warning(f"Correlation tracking timed out for event {event_id}")
            except Exception as e:
                logger.error(f"Correlation tracking failed for event {event_id}: {e}")

        # Prepare message
        payload = orjson.dumps(body)
        msg = aio_pika.Message(
            payload,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=str(event_id) if event_id else (message_id or body.get("id")),
            correlation_id=correlation_id or body.get("correlation_id"),
            content_type="application/json",
            content_encoding="utf-8",
        )

        # Publish
        await self._exchange.publish(msg, routing_key=routing_key)
        logger.debug(f"Published message to {routing_key}: {msg.message_id}")

    def generate_event_id(self, event_type: str, **unique_fields) -> UUID:
        """
        Generate deterministic event ID for idempotency (sync operation).

        Same event_type + unique_fields will always generate the same UUID.
        This allows consumers to dedupe based on event_id.

        Args:
            event_type: The event type (e.g., "fireflies.transcript.upload")
            **unique_fields: Key-value pairs that uniquely identify this event

        Returns:
            Deterministic UUID

        Example:
            >>> event_id = publisher.generate_event_id(
            ...     "fireflies.transcript.upload",
            ...     meeting_id="abc123",
            ...     user_id="user_456"
            ... )
            >>> # Same inputs will always generate same UUID

        Raises:
            RuntimeError: If correlation tracking is disabled
        """
        if not self.enable_correlation_tracking or not self.tracker:
            raise RuntimeError(
                "Correlation tracking is disabled. "
                "Initialize Publisher with enable_correlation_tracking=True"
            )

        # Sort fields for consistency
        sorted_fields = sorted(unique_fields.items())
        unique_key = "|".join(f"{k}={v}" for k, v in sorted_fields)

        return self.tracker.generate_event_id(event_type, unique_key)

    async def get_correlation_chain(
        self, event_id: UUID, direction: str = "ancestors"
    ) -> List[UUID]:
        """
        Get full correlation chain for an event.

        Args:
            event_id: Event to look up
            direction: "ancestors" or "descendants"

        Returns:
            List of event UUIDs in the chain

        Raises:
            RuntimeError: If correlation tracking is disabled
        """
        if not self.enable_correlation_tracking or not self.tracker:
            raise RuntimeError("Correlation tracking is disabled")

        return await self.tracker.get_correlation_chain(event_id, direction)

    async def debug_correlation(self, event_id: UUID) -> Dict[str, Any]:
        """
        Debug dump of correlation data for an event.

        Args:
            event_id: Event to debug

        Returns:
            Dict with parents, children, ancestors, descendants

        Raises:
            RuntimeError: If correlation tracking is disabled
        """
        if not self.enable_correlation_tracking or not self.tracker:
            raise RuntimeError("Correlation tracking is disabled")

        return await self.tracker.debug_dump(event_id)

    async def close(self):
        """Close connections gracefully."""
        async with self._lock:
            # Close tracker first
            if self.tracker:
                try:
                    await self.tracker.close()
                except Exception as e:
                    logger.error(f"Error closing correlation tracker: {e}")

            # Close RabbitMQ connection
            if self._channel and not self._channel.is_closed:
                await self._channel.close()
            if self._conn:
                await self._conn.close()

            self._conn = None
            self._channel = None
            self._exchange = None
            self._started = False
            logger.info("Publisher: Closed all connections")
