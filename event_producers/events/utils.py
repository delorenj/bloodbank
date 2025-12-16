"""
Utility functions for event envelope creation and management.
"""

from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import UUID, uuid4

from .base import AgentContext, EventEnvelope, Source


def create_envelope(
    event_type: str,
    payload: Any,
    source: Source,
    correlation_ids: Optional[List[UUID]] = None,
    agent_context: Optional[AgentContext] = None,
    event_id: Optional[UUID] = None,
) -> EventEnvelope:
    """
    Helper to create properly-formed event envelope.

    Args:
        event_type: Routing key (e.g., "fireflies.transcript.ready")
        payload: Your typed payload
        source: Source metadata
        correlation_ids: List of parent event IDs (for causation tracking)
        agent_context: Agent metadata (if source.type == AGENT)
        event_id: Optional explicit event ID (for deterministic IDs)

    Returns:
        EventEnvelope with proper typing
    """
    return EventEnvelope(
        event_id=event_id or uuid4(),
        event_type=event_type,
        timestamp=datetime.now(timezone.utc),
        version="1.0.0",
        source=source,
        correlation_ids=correlation_ids or [],
        agent_context=agent_context,
        payload=payload,
    )
