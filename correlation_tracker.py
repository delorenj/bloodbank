"""
Async correlation ID tracker using Redis for state management.

This module provides functionality to:
1. Generate deterministic event IDs for idempotency
2. Track correlation chains (parent → child event relationships)
3. Query correlation history for debugging

Usage:
    tracker = CorrelationTracker()
    await tracker.start()

    # Generate deterministic event ID (sync operation)
    event_id = tracker.generate_event_id(
        event_type="fireflies.transcript.upload",
        unique_key="meeting_abc123"
    )

    # Track correlation when publishing follow-up event
    await tracker.add_correlation(
        child_event_id=new_event_id,
        parent_event_ids=[original_event_id]
    )

    # Query correlation chain
    chain = await tracker.get_correlation_chain(event_id)
"""

import orjson
from typing import List, Optional, Dict, Any
from uuid import UUID, uuid5, NAMESPACE_OID
from datetime import datetime
import redis.asyncio as redis
from redis.exceptions import RedisError
import asyncio
import logging

logger = logging.getLogger(__name__)


class CorrelationTracker:
    """Async Redis-backed correlation tracker for event causation chains."""

    def __init__(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_db: int = 0,
        redis_password: Optional[str] = None,
        ttl_days: int = 30,
        max_retries: int = 3,
        connection_timeout: float = 5.0,
    ):
        """
        Initialize correlation tracker.

        Args:
            redis_host: Redis server hostname
            redis_port: Redis server port
            redis_db: Redis database number
            redis_password: Redis password (if required)
            ttl_days: How long to keep correlation data (default 30 days)
            max_retries: Maximum retry attempts for Redis operations
            connection_timeout: Timeout for Redis operations (seconds)
        """
        self.redis_url = f"redis://{redis_host}:{redis_port}/{redis_db}"
        self.redis_password = redis_password
        self.ttl_seconds = ttl_days * 86400
        self.max_retries = max_retries
        self.connection_timeout = connection_timeout
        self.redis: Optional[redis.Redis] = None
        self._started = False
        self._lock = asyncio.Lock()

    async def start(self):
        """Initialize Redis connection pool."""
        if self._started:
            return

        async with self._lock:
            if self._started:
                return

            try:
                self.redis = await redis.from_url(
                    self.redis_url,
                    password=self.redis_password,
                    decode_responses=True,
                    socket_timeout=self.connection_timeout,
                    socket_connect_timeout=self.connection_timeout,
                )
                # Test connection
                await asyncio.wait_for(
                    self.redis.ping(), timeout=self.connection_timeout
                )
                self._started = True
                logger.info("CorrelationTracker: Redis connection established")
            except (RedisError, asyncio.TimeoutError, ConnectionError) as e:
                logger.error(f"Failed to connect to Redis: {e}")
                # Don't raise - allow graceful degradation
                self.redis = None
                self._started = False

    async def close(self):
        """Close Redis connection."""
        async with self._lock:
            if self.redis:
                await self.redis.aclose()
                self.redis = None
                self._started = False
                logger.info("CorrelationTracker: Redis connection closed")

    def generate_event_id(
        self, event_type: str, unique_key: str, namespace: str = "bloodbank"
    ) -> UUID:
        """
        Generate deterministic UUID for idempotency (sync operation).

        Same event_type + unique_key will always generate the same UUID.
        This allows consumers to dedupe based on event_id.

        Args:
            event_type: The event type (e.g., "fireflies.transcript.upload")
            unique_key: Unique identifier for this specific event instance
                       (e.g., meeting ID, file path, etc.)
            namespace: Optional namespace to prevent collisions (default: "bloodbank")

        Returns:
            Deterministic UUID v5

        Example:
            >>> tracker.generate_event_id(
            ...     "fireflies.transcript.upload",
            ...     "meeting_abc123"
            ... )
            UUID('5a3c5b8d-...')  # Always the same for this combination
        """
        # Create deterministic string
        deterministic_str = f"{namespace}:{event_type}:{unique_key}"

        # Generate UUID v5 (SHA-1 based, deterministic)
        # Use NAMESPACE_OID as the namespace UUID
        event_id = uuid5(NAMESPACE_OID, deterministic_str)

        return event_id

    async def add_correlation(
        self,
        child_event_id: UUID,
        parent_event_ids: List[UUID],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Record correlation between child event and parent event(s).

        Args:
            child_event_id: The new event being published
            parent_event_ids: List of event IDs that caused this event
            metadata: Optional metadata about the correlation

        Example:
            >>> # Event B is caused by Event A
            >>> await tracker.add_correlation(
            ...     child_event_id=event_b_id,
            ...     parent_event_ids=[event_a_id]
            ... )

            >>> # Event C is caused by both A and B
            >>> await tracker.add_correlation(
            ...     child_event_id=event_c_id,
            ...     parent_event_ids=[event_a_id, event_b_id]
            ... )
        """
        if not self._started or not self.redis:
            logger.warning(
                "CorrelationTracker not started, skipping correlation tracking"
            )
            return

        try:
            child_id_str = str(child_event_id)

            # Use pipeline for atomic operations
            async with self.redis.pipeline(transaction=True) as pipe:
                # Store forward mapping: child → parents
                key_forward = f"bloodbank:correlation:forward:{child_id_str}"
                correlation_data = {
                    "parent_event_ids": [str(pid) for pid in parent_event_ids],
                    "created_at": datetime.utcnow().isoformat(),
                    "metadata": metadata or {},
                }

                await pipe.setex(
                    key_forward,
                    self.ttl_seconds,
                    orjson.dumps(correlation_data).decode(),
                )

                # Store reverse mappings: parent → children (for querying)
                for parent_id in parent_event_ids:
                    parent_id_str = str(parent_id)
                    key_reverse = f"bloodbank:correlation:reverse:{parent_id_str}"

                    await pipe.sadd(key_reverse, child_id_str)
                    await pipe.expire(key_reverse, self.ttl_seconds)

                await pipe.execute()

        except (RedisError, asyncio.TimeoutError) as e:
            # Log error but don't raise - correlation tracking is non-critical
            logger.error(f"Failed to add correlation for {child_event_id}: {e}")

    async def get_parents(self, event_id: UUID) -> List[UUID]:
        """
        Get immediate parent event IDs.

        Args:
            event_id: The event to look up

        Returns:
            List of parent event IDs (empty if no parents)
        """
        if not self._started or not self.redis:
            return []

        try:
            key = f"bloodbank:correlation:forward:{str(event_id)}"
            data = await asyncio.wait_for(
                self.redis.get(key), timeout=self.connection_timeout
            )

            if not data:
                return []

            correlation = orjson.loads(data)
            return [UUID(pid) for pid in correlation["parent_event_ids"]]

        except (RedisError, asyncio.TimeoutError, KeyError, ValueError) as e:
            logger.error(f"Failed to get parents for {event_id}: {e}")
            return []

    async def get_children(self, event_id: UUID) -> List[UUID]:
        """
        Get immediate child event IDs.

        Args:
            event_id: The event to look up

        Returns:
            List of child event IDs (empty if no children)
        """
        if not self._started or not self.redis:
            return []

        try:
            key = f"bloodbank:correlation:reverse:{str(event_id)}"
            child_ids = await asyncio.wait_for(
                self.redis.smembers(key), timeout=self.connection_timeout
            )

            return [UUID(cid) for cid in child_ids]

        except (RedisError, asyncio.TimeoutError, ValueError) as e:
            logger.error(f"Failed to get children for {event_id}: {e}")
            return []

    async def get_correlation_chain(
        self, event_id: UUID, direction: str = "ancestors", max_depth: int = 100
    ) -> List[UUID]:
        """
        Get full correlation chain (all ancestors or descendants).

        Args:
            event_id: Starting event
            direction: "ancestors" (parents, grandparents, etc.) or
                      "descendants" (children, grandchildren, etc.)
            max_depth: Maximum traversal depth to prevent infinite loops

        Returns:
            List of event IDs in the chain (topologically sorted)

        Example:
            >>> # Given chain: A → B → C → D
            >>> await tracker.get_correlation_chain(UUID("C"), "ancestors")
            [UUID("A"), UUID("B"), UUID("C")]

            >>> await tracker.get_correlation_chain(UUID("B"), "descendants")
            [UUID("B"), UUID("C"), UUID("D")]
        """
        if not self._started or not self.redis:
            return []

        visited = set()
        chain = []
        depth = 0

        async def traverse(eid: UUID, current_depth: int):
            nonlocal depth

            if eid in visited or current_depth >= max_depth:
                return

            visited.add(eid)
            depth = max(depth, current_depth)

            if direction == "ancestors":
                parents = await self.get_parents(eid)
                for parent in parents:
                    await traverse(parent, current_depth + 1)

            elif direction == "descendants":
                children = await self.get_children(eid)
                for child in children:
                    await traverse(child, current_depth + 1)

            # Only add event if it's not the starting event OR if starting event has correlations
            if eid != event_id:
                chain.append(eid)
            elif direction == "ancestors" and await self.get_parents(eid):
                chain.append(eid)
            elif direction == "descendants" and await self.get_children(eid):
                chain.append(eid)

        try:
            await traverse(event_id, 0)
            return chain
        except Exception as e:
            logger.error(f"Failed to get correlation chain for {event_id}: {e}")
            return []

    async def get_correlation_metadata(
        self, event_id: UUID
    ) -> Optional[Dict[str, Any]]:
        """
        Get metadata about a correlation relationship.

        Args:
            event_id: The child event

        Returns:
            Metadata dict or None if not found
        """
        if not self._started or not self.redis:
            return None

        try:
            key = f"bloodbank:correlation:forward:{str(event_id)}"
            data = await asyncio.wait_for(
                self.redis.get(key), timeout=self.connection_timeout
            )

            if not data:
                return None

            correlation = orjson.loads(data)
            return correlation.get("metadata")

        except (RedisError, asyncio.TimeoutError, KeyError) as e:
            logger.error(f"Failed to get metadata for {event_id}: {e}")
            return None

    async def debug_dump(self, event_id: UUID) -> Dict[str, Any]:
        """
        Dump all correlation data for debugging.

        Args:
            event_id: Event to debug

        Returns:
            Dict with parents, children, ancestors, descendants
        """
        return {
            "event_id": str(event_id),
            "parents": [str(p) for p in await self.get_parents(event_id)],
            "children": [str(c) for c in await self.get_children(event_id)],
            "ancestors": [
                str(a) for a in await self.get_correlation_chain(event_id, "ancestors")
            ],
            "descendants": [
                str(d)
                for d in await self.get_correlation_chain(event_id, "descendants")
            ],
            "metadata": await self.get_correlation_metadata(event_id),
        }


# Convenience functions for common patterns


async def link_events(
    tracker: CorrelationTracker, parent: UUID, child: UUID, reason: Optional[str] = None
) -> None:
    """
    Convenience function to link two events.

    Args:
        tracker: CorrelationTracker instance
        parent: Parent event ID
        child: Child event ID
        reason: Optional description of the relationship
    """
    metadata = {"reason": reason} if reason else None
    await tracker.add_correlation(child, [parent], metadata)


def generate_idempotent_id(
    tracker: CorrelationTracker, event_type: str, **unique_fields
) -> UUID:
    """
    Generate idempotent event ID from event type and unique fields (sync).

    Args:
        tracker: CorrelationTracker instance
        event_type: Event type string
        **unique_fields: Key-value pairs that uniquely identify this event

    Returns:
        Deterministic UUID

    Example:
        >>> generate_idempotent_id(
        ...     tracker,
        ...     "fireflies.transcript.upload",
        ...     meeting_id="abc123",
        ...     user_id="user_456"
        ... )
    """
    # Sort fields for consistency
    sorted_fields = sorted(unique_fields.items())
    unique_key = "|".join(f"{k}={v}" for k, v in sorted_fields)

    return tracker.generate_event_id(event_type, unique_key)
