"""
Core event envelope types for Bloodbank event bus.

This module contains the fundamental building blocks for all events:
- EventEnvelope: Generic wrapper for all event payloads
- TriggerType, Source: Event origin metadata
- AgentType, AgentContext, CodeState: AI agent context tracking

All events in the system are wrapped in EventEnvelope[T] where T is your payload type.
"""

from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Generic, TypeVar
from uuid import UUID, uuid4
from enum import Enum


# ============================================================================
# Core Envelope Types
# ============================================================================


class TriggerType(str, Enum):
    """How was this event triggered?"""

    MANUAL = "manual"  # Human-initiated
    AGENT = "agent"  # AI agent triggered
    SCHEDULED = "scheduled"  # Cron/timer triggered
    FILE_WATCH = "file_watch"  # File system event
    HOOK = "hook"  # External webhook


class Source(BaseModel):
    """Identifies WHO or WHAT triggered the event."""

    host: str  # Machine that generated event
    type: TriggerType  # How was this triggered?
    app: Optional[str] = None  # Application name
    meta: Optional[Dict[str, Any]] = None  # Additional context


class AgentType(str, Enum):
    """Known agent types in the 33GOD ecosystem."""

    CLAUDE_CODE = "claude-code"
    CLAUDE_CHAT = "claude-chat"
    GEMINI_CLI = "gemini-cli"
    GEMINI_CODE = "gemini-code"
    LETTA = "letta"
    AGNO = "agno"
    SMOLAGENT = "smolagent"
    ATOMIC_AGENT = "atomic-agent"
    CUSTOM = "custom"


class CodeState(BaseModel):
    """Git context for agent's working environment."""

    repo_url: Optional[str] = None
    branch: Optional[str] = None
    working_diff: Optional[str] = None  # Unstaged changes
    branch_diff: Optional[str] = None  # Diff vs main
    last_commit_hash: Optional[str] = None


class AgentContext(BaseModel):
    """Rich metadata about the AI agent (when source.type == AGENT)."""

    type: AgentType
    name: Optional[str] = None  # Agent's persona/name
    system_prompt: Optional[str] = None  # Initial system prompt
    instance_id: Optional[str] = None  # Unique session identifier
    mcp_servers: Optional[List[str]] = None  # Connected MCP servers
    file_references: Optional[List[str]] = None  # Files in context
    url_references: Optional[List[str]] = None  # URLs in context
    code_state: Optional[CodeState] = None  # Git state snapshot
    checkpoint_id: Optional[str] = None  # For checkpoint-based agents
    meta: Optional[Dict[str, Any]] = None  # Extensibility


T = TypeVar("T")


class EventEnvelope(BaseModel, Generic[T]):
    """
    Generic event envelope that wraps all events.

    Versioning Strategy:
    - Bump 'version' field for breaking changes to envelope structure
    - Payload schemas can evolve independently (add optional fields)
    - For breaking payload changes, create new event type (e.g., .v2)
    """

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str  # Routing key (e.g., "fireflies.transcript.ready")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0.0"  # Envelope schema version
    source: Source  # Who/what triggered this
    correlation_ids: List[UUID] = Field(default_factory=list)  # Parent event IDs
    agent_context: Optional[AgentContext] = None  # Agent metadata (if applicable)
    payload: T  # Your typed event data

    model_config = ConfigDict(
        # Pydantic v2 handles UUID and datetime serialization automatically
        # No need for json_encoders
    )


# ============================================================================
# Helper Functions
# ============================================================================


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

    Example:
        >>> from event_producers.events.base import create_envelope, Source, TriggerType
        >>> source = Source(host="localhost", type=TriggerType.MANUAL)
        >>> envelope = create_envelope(
        ...     event_type="test.event",
        ...     payload={"data": "test"},
        ...     source=source
        ... )
        >>> envelope.event_type
        'test.event'
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
