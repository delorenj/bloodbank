"""
Type-safe event type definitions for Bloodbank.

This module provides Literal types for all registered event types,
enabling compile-time checking of event type references in consumers.

Usage:
    from event_producers.events.types import FirefliesEventTypes

    @EventConsumer.event_handler(FirefliesEventTypes.TRANSCRIPT_READY)
    async def handle_ready(self, envelope: EventEnvelope):
        ...

    # Type narrowing for payload validation
    if envelope.event_type == FirefliesEventTypes.TRANSCRIPT_READY:
        payload: FirefliesTranscriptReadyPayload = envelope.payload
"""

from typing import Literal


# =============================================================================
# Fireflies Domain Events
# =============================================================================

FirefliesEventType = Literal[
    "fireflies.transcript.upload",
    "fireflies.transcript.ready",
    "fireflies.transcript.processed",
    "fireflies.transcript.failed",
]


# =============================================================================
# Agent Domain Events
# =============================================================================

AgentThreadEventType = Literal[
    "agent.thread.prompt",
    "agent.thread.response",
    "agent.thread.error",
]

AgentFeedbackEventType = Literal[
    "agent.feedback.requested",
    "agent.feedback.response",
]

AgentOpenClawEventType = Literal[
    "agent.message.received",
    "agent.message.sent",
    "agent.tool.invoked",
    "agent.tool.completed",
    "agent.subagent.spawned",
    "agent.subagent.completed",
    "agent.session.started",
    "agent.session.ended",
    "agent.task.assigned",
    "agent.task.completed",
    "agent.heartbeat",
    "agent.error",
]


# =============================================================================
# GitHub Domain Events
# =============================================================================

GitHubEventType = Literal["github.pr.created",]


# =============================================================================
# TheBoard Domain Events
# =============================================================================

TheBoardEventType = Literal[
    "meeting.created",
    "meeting.started",
    "meeting.completed",
    "meeting.failed",
    "meeting.converged",
    "round.completed",
    "participant.added",
    "participant.turn.completed",
    "comment.extracted",
]


# =============================================================================
# LLM Domain Events
# =============================================================================

LLMEventType = Literal[
    "llm.prompt",
    "llm.response",
    "llm.error",
]


# =============================================================================
# Artifact Domain Events
# =============================================================================

ArtifactEventType = Literal["artifact.ingestion.failed",]


# =============================================================================
# All Events Union
# =============================================================================

BloodbankEventType = Literal[
    # Fireflies
    "fireflies.transcript.upload",
    "fireflies.transcript.ready",
    "fireflies.transcript.processed",
    "fireflies.transcript.failed",
    # Agent
    "agent.thread.prompt",
    "agent.thread.response",
    "agent.thread.error",
    "agent.feedback.requested",
    "agent.feedback.response",
    # Agent OpenClaw
    "agent.message.received",
    "agent.message.sent",
    "agent.tool.invoked",
    "agent.tool.completed",
    "agent.subagent.spawned",
    "agent.subagent.completed",
    "agent.session.started",
    "agent.session.ended",
    "agent.task.assigned",
    "agent.task.completed",
    "agent.heartbeat",
    "agent.error",
    # GitHub
    "github.pr.created",
    # TheBoard
    "meeting.created",
    "meeting.started",
    "meeting.completed",
    "meeting.failed",
    "meeting.converged",
    "round.completed",
    "participant.added",
    "participant.turn.completed",
    "comment.extracted",
    # LLM
    "llm.prompt",
    "llm.response",
    "llm.error",
    # Artifact
    "artifact.ingestion.failed",
]


# =============================================================================
# Event Type to Payload Mapping
# =============================================================================

from event_producers.events.domains.fireflies import (
    FirefliesTranscriptUploadPayload,
    FirefliesTranscriptReadyPayload,
    FirefliesTranscriptProcessedPayload,
    FirefliesTranscriptFailedPayload,
)
from event_producers.events.domains.agent.feedback import (
    AgentFeedbackRequested,
    AgentFeedbackResponse,
)
from event_producers.events.domains.agent.thread import (
    AgentThreadPrompt,
    AgentThreadResponse,
    AgentThreadErrorPayload,
)
from event_producers.events.domains.agent.openclaw import (
    AgentMessageReceived,
    AgentMessageSent,
    AgentToolInvoked,
    AgentToolCompleted,
    AgentSubagentSpawned,
    AgentSubagentCompleted,
    AgentSessionStarted,
    AgentSessionEnded,
    AgentTaskAssigned,
    AgentTaskCompleted,
    AgentHeartbeat,
    AgentError,
)
from event_producers.events.domains.github import GitHubPRCreatedPayload
from event_producers.events.domains.theboard import (
    MeetingCreatedPayload,
    MeetingStartedPayload,
    MeetingCompletedPayload,
    MeetingFailedPayload,
    MeetingConvergedPayload,
    RoundCompletedPayload,
    ParticipantAddedPayload,
    ParticipantTurnCompletedPayload,
    CommentExtractedPayload,
)
from event_producers.events.domains.llm import (
    LLMPrompt,
    LLMResponse,
    LLMErrorPayload,
)
from event_producers.events.domains.artifact import (
    Artifact,
    ArtifactIngestionFailedPayload,
)
from typing import Type, Dict, Any


# Maps event type strings to their payload classes
# Used for runtime validation and type checking
EVENT_TYPE_TO_PAYLOAD: Dict[str, Type[Any]] = {
    # Fireflies
    "fireflies.transcript.upload": FirefliesTranscriptUploadPayload,
    "fireflies.transcript.ready": FirefliesTranscriptReadyPayload,
    "fireflies.transcript.processed": FirefliesTranscriptProcessedPayload,
    "fireflies.transcript.failed": FirefliesTranscriptFailedPayload,
    # Agent
    "agent.thread.prompt": AgentThreadPrompt,
    "agent.thread.response": AgentThreadResponse,
    "agent.thread.error": AgentThreadErrorPayload,
    "agent.feedback.requested": AgentFeedbackRequested,
    "agent.feedback.response": AgentFeedbackResponse,
    # Agent OpenClaw
    "agent.message.received": AgentMessageReceived,
    "agent.message.sent": AgentMessageSent,
    "agent.tool.invoked": AgentToolInvoked,
    "agent.tool.completed": AgentToolCompleted,
    "agent.subagent.spawned": AgentSubagentSpawned,
    "agent.subagent.completed": AgentSubagentCompleted,
    "agent.session.started": AgentSessionStarted,
    "agent.session.ended": AgentSessionEnded,
    "agent.task.assigned": AgentTaskAssigned,
    "agent.task.completed": AgentTaskCompleted,
    "agent.heartbeat": AgentHeartbeat,
    "agent.error": AgentError,
    # GitHub
    "github.pr.created": GitHubPRCreatedPayload,
    # TheBoard
    "meeting.created": MeetingCreatedPayload,
    "meeting.started": MeetingStartedPayload,
    "meeting.completed": MeetingCompletedPayload,
    "meeting.failed": MeetingFailedPayload,
    "meeting.converged": MeetingConvergedPayload,
    "round.completed": RoundCompletedPayload,
    "participant.added": ParticipantAddedPayload,
    "participant.turn.completed": ParticipantTurnCompletedPayload,
    "comment.extracted": CommentExtractedPayload,
    # LLM
    "llm.prompt": LLMPrompt,
    "llm.response": LLMResponse,
    "llm.error": LLMErrorPayload,
    # Artifact
    "artifact.ingestion.failed": ArtifactIngestionFailedPayload,
}


def get_payload_class(event_type: str) -> Type[Any]:
    """
    Get the payload class for an event type.

    Args:
        event_type: The event type string (e.g., "fireflies.transcript.ready")

    Returns:
        The payload class for the event type

    Raises:
        KeyError: If event_type is not registered
    """
    return EVENT_TYPE_TO_PAYLOAD[event_type]


# =============================================================================
# Event Type Metadata
# =============================================================================

from enum import Enum


class EventCategory(str, Enum):
    """Broad categorization of event types."""

    INFRASTRUCTURE = "infrastructure"
    LLM = "llm"
    AGENT = "agent"
    EXTERNAL = "external"


# Note: Due to Python's limitations with complex literal types,
# we provide helper functions for runtime type checking


def get_event_category(event_type: str) -> EventCategory:
    """
    Get the category for an event type.

    Args:
        event_type: The event type string (e.g., "fireflies.transcript.ready")

    Returns:
        EventCategory enum value

    Raises:
        ValueError: If event_type is not recognized
    """
    prefixes = {
        "llm.": EventCategory.LLM,
        "agent.": EventCategory.AGENT,
        "fireflies.": EventCategory.EXTERNAL,
        "github.": EventCategory.EXTERNAL,
        "meeting.": EventCategory.EXTERNAL,
        "round.": EventCategory.EXTERNAL,
        "participant.": EventCategory.EXTERNAL,
        "comment.": EventCategory.EXTERNAL,
        "artifact.": EventCategory.INFRASTRUCTURE,
    }

    for prefix, category in sorted(prefixes.items(), key=lambda x: -len(x[0])):
        if event_type.startswith(prefix):
            return category

    raise ValueError(f"Unknown event type prefix: {event_type}")
