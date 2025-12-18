"""
RabbitMQ publisher with built-in correlation tracking.

This module provides the Publisher class for connecting to and publishing
messages to RabbitMQ with automatic correlation tracking via Redis.
"""

import json
from typing import Any, Dict, List, Optional
from uuid import UUID

import aio_pika

from .config import settings
from .correlation_tracker import CorrelationTracker


class Publisher:
    """
    RabbitMQ publisher with correlation tracking.

    Features:
    - Automatic correlation tracking via Redis
    - Deterministic event ID generation for idempotency
    - Persistent message publishing
    - Async/await support

    Usage:
        publisher = Publisher()
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
        enable_correlation_tracking: bool = True,
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
        self.connection = None
        self.channel = None
        self.exchange = None

        # Correlation tracking
        self.enable_correlation_tracking = enable_correlation_tracking
        if enable_correlation_tracking:
            self.tracker = CorrelationTracker(
                redis_host=redis_host or getattr(settings, "redis_host", "localhost"),
                redis_port=redis_port or getattr(settings, "redis_port", 6379),
                redis_password=redis_password or getattr(settings, "redis_password", None),
            )
        else:
            self.tracker = None

    async def start(self):
        """Connect to RabbitMQ and declare exchange."""
        self.connection = await aio_pika.connect_robust(settings.rabbit_url)
        self.channel = await self.connection.channel()

        # Declare the topic exchange
        self.exchange = await self.channel.declare_exchange(
            settings.exchange_name, aio_pika.ExchangeType.TOPIC, durable=True
        )

    async def publish(
        self,
        routing_key: str,
        body: Dict[str, Any],
        message_id: Optional[str] = None,
        event_id: Optional[UUID] = None,
        parent_event_ids: Optional[List[UUID]] = None,
        correlation_metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        """
        Publish a message to the exchange with correlation tracking.

        Args:
            routing_key: RabbitMQ routing key (should match event_type)
            body: Message body (dict, will be JSON serialized)
            message_id: DEPRECATED - use event_id instead
            event_id: Event UUID (extracted from body if not provided)
            parent_event_ids: List of parent event UUIDs for correlation
            correlation_metadata: Optional metadata about the correlation
            **kwargs: Additional properties for aio_pika.Message

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
        if not self.exchange:
            raise RuntimeError("Publisher not started. Call await publisher.start() first.")

        # Extract event_id from body if not provided
        if event_id is None:
            event_id_str = body.get("event_id")
            if event_id_str:
                event_id = UUID(event_id_str) if isinstance(event_id_str, str) else event_id_str

        # Track correlation if enabled
        if self.enable_correlation_tracking and self.tracker and event_id:
            if parent_event_ids:
                self.tracker.add_correlation(
                    child_event_id=event_id,
                    parent_event_ids=parent_event_ids,
                    metadata=correlation_metadata,
                )

        # Prepare message
        message = aio_pika.Message(
            body=json.dumps(body).encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,  # Survive broker restart
            message_id=str(event_id) if event_id else message_id,
            **kwargs,
        )

        # Publish
        await self.exchange.publish(message, routing_key=routing_key)

    def generate_event_id(self, event_type: str, **unique_fields) -> UUID:
        """
        Generate deterministic event ID for idempotency.

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

    def get_correlation_chain(self, event_id: UUID, direction: str = "ancestors") -> List[UUID]:
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

        return self.tracker.get_correlation_chain(event_id, direction)

    def debug_correlation(self, event_id: UUID) -> Dict[str, Any]:
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

        return self.tracker.debug_dump(event_id)

    async def close(self):
        """Close connection gracefully."""
        if self.channel:
            await self.channel.close()
        if self.connection:
            await self.connection.close()
