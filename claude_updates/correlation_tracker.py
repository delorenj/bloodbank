"""
Correlation ID tracker using Redis for state management.

This module provides functionality to:
1. Generate deterministic event IDs for idempotency
2. Track correlation chains (parent → child event relationships)
3. Query correlation history for debugging

Usage:
    tracker = CorrelationTracker()

    # Generate deterministic event ID
    event_id = tracker.generate_event_id(
        event_type="fireflies.transcript.upload",
        unique_key="meeting_abc123"
    )

    # Track correlation when publishing follow-up event
    tracker.add_correlation(
        child_event_id=new_event_id,
        parent_event_ids=[original_event_id]
    )

    # Query correlation chain
    chain = tracker.get_correlation_chain(event_id)
"""

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

import redis
from redis.exceptions import RedisError


class CorrelationTracker:
    """Redis-backed correlation tracker for event causation chains."""

    def __init__(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_db: int = 0,
        redis_password: Optional[str] = None,
        ttl_days: int = 30,
    ):
        """
        Initialize correlation tracker.

        Args:
            redis_host: Redis server hostname
            redis_port: Redis server port
            redis_db: Redis database number
            redis_password: Redis password (if required)
            ttl_days: How long to keep correlation data (default 30 days)
        """
        self.redis = redis.Redis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            password=redis_password,
            decode_responses=True,
        )
        self.ttl_seconds = ttl_days * 86400

        # Test connection
        try:
            self.redis.ping()
        except RedisError as e:
            raise ConnectionError(f"Failed to connect to Redis: {e}")

    def generate_event_id(
        self, event_type: str, unique_key: str, namespace: str = "bloodbank"
    ) -> UUID:
        """
        Generate deterministic UUID for idempotency.

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
        event_id = UUID(bytes=hashlib.sha1(deterministic_str.encode()).digest()[:16], version=5)

        return event_id

    def add_correlation(
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
            >>> tracker.add_correlation(
            ...     child_event_id=event_b_id,
            ...     parent_event_ids=[event_a_id]
            ... )

            >>> # Event C is caused by both A and B
            >>> tracker.add_correlation(
            ...     child_event_id=event_c_id,
            ...     parent_event_ids=[event_a_id, event_b_id]
            ... )
        """
        child_id_str = str(child_event_id)

        # Store forward mapping: child → parents
        key_forward = f"bloodbank:correlation:forward:{child_id_str}"
        correlation_data = {
            "parent_event_ids": [str(pid) for pid in parent_event_ids],
            "created_at": datetime.utcnow().isoformat(),
            "metadata": metadata or {},
        }
        self.redis.setex(key_forward, self.ttl_seconds, json.dumps(correlation_data))

        # Store reverse mappings: parent → children (for querying)
        for parent_id in parent_event_ids:
            parent_id_str = str(parent_id)
            key_reverse = f"bloodbank:correlation:reverse:{parent_id_str}"

            # Add to set of children
            self.redis.sadd(key_reverse, child_id_str)
            self.redis.expire(key_reverse, self.ttl_seconds)

    def get_parents(self, event_id: UUID) -> List[UUID]:
        """
        Get immediate parent event IDs.

        Args:
            event_id: The event to look up

        Returns:
            List of parent event IDs (empty if no parents)
        """
        key = f"bloodbank:correlation:forward:{str(event_id)}"
        data = self.redis.get(key)

        if not data:
            return []

        correlation = json.loads(data)
        return [UUID(pid) for pid in correlation["parent_event_ids"]]

    def get_children(self, event_id: UUID) -> List[UUID]:
        """
        Get immediate child event IDs.

        Args:
            event_id: The event to look up

        Returns:
            List of child event IDs (empty if no children)
        """
        key = f"bloodbank:correlation:reverse:{str(event_id)}"
        child_ids = self.redis.smembers(key)

        return [UUID(cid) for cid in child_ids]

    def get_correlation_chain(self, event_id: UUID, direction: str = "ancestors") -> List[UUID]:
        """
        Get full correlation chain (all ancestors or descendants).

        Args:
            event_id: Starting event
            direction: "ancestors" (parents, grandparents, etc.) or
                      "descendants" (children, grandchildren, etc.)

        Returns:
            List of event IDs in the chain (excluding the event itself if no correlations)

        Example:
            >>> # Given chain: A → B → C → D
            >>> tracker.get_correlation_chain(UUID("C"), "ancestors")
            [UUID("A"), UUID("B")]

            >>> tracker.get_correlation_chain(UUID("B"), "descendants")
            [UUID("C"), UUID("D")]
        """
        visited = set()
        chain = []

        def traverse(eid: UUID):
            if eid in visited:
                return
            visited.add(eid)

            if direction == "ancestors":
                parents = self.get_parents(eid)
                for parent in parents:
                    traverse(parent)
                # Add this event if it's not the starting event and has parents
                if eid != event_id and parents:
                    chain.append(eid)

            elif direction == "descendants":
                children = self.get_children(eid)
                for child in children:
                    traverse(child)
                # Add this event if it's not the starting event and has children
                if eid != event_id and children:
                    chain.append(eid)

        traverse(event_id)
        return chain

    def get_correlation_metadata(self, event_id: UUID) -> Optional[Dict[str, Any]]:
        """
        Get metadata about a correlation relationship.

        Args:
            event_id: The child event

        Returns:
            Metadata dict or None if not found
        """
        key = f"bloodbank:correlation:forward:{str(event_id)}"
        data = self.redis.get(key)

        if not data:
            return None

        correlation = json.loads(data)
        return correlation.get("metadata")

    def cleanup_expired(self) -> int:
        """
        Manually clean up expired correlation data.

        Note: Redis should handle this automatically via TTL,
        but this can be useful for maintenance.

        Returns:
            Number of keys deleted
        """
        pattern = "bloodbank:correlation:*"
        cursor = 0
        deleted = 0

        while True:
            cursor, keys = self.redis.scan(cursor, match=pattern, count=100)

            for key in keys:
                ttl = self.redis.ttl(key)
                if ttl == -1:  # No expiry set
                    self.redis.expire(key, self.ttl_seconds)
                elif ttl == -2:  # Key doesn't exist
                    deleted += 1

            if cursor == 0:
                break

        return deleted

    def debug_dump(self, event_id: UUID) -> Dict[str, Any]:
        """
        Dump all correlation data for debugging.

        Args:
            event_id: Event to debug

        Returns:
            Dict with parents, children, ancestors, descendants
        """
        return {
            "event_id": str(event_id),
            "parents": [str(p) for p in self.get_parents(event_id)],
            "children": [str(c) for c in self.get_children(event_id)],
            "ancestors": [str(a) for a in self.get_correlation_chain(event_id, "ancestors")],
            "descendants": [str(d) for d in self.get_correlation_chain(event_id, "descendants")],
            "metadata": self.get_correlation_metadata(event_id),
        }


# Convenience functions for common patterns


def link_events(
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
    tracker.add_correlation(child, [parent], metadata)


def generate_idempotent_id(tracker: CorrelationTracker, event_type: str, **unique_fields) -> UUID:
    """
    Generate idempotent event ID from event type and unique fields.

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
