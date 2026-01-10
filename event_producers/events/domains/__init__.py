"""
Event domain payload definitions.

This package contains domain-specific event payload classes organized by domain:
- agent_thread: Agent interaction events
- fireflies: Fireflies transcription events
- github: GitHub integration events

Each domain module exports:
- Payload classes (e.g., FirefliesTranscriptReadyPayload)
- ROUTING_KEYS dictionary mapping class names to routing keys

Domain Auto-Discovery:
This package is automatically scanned by the EventRegistry during initialization.
The registry discovers all modules in this package, imports them, and registers
their event types based on the ROUTING_KEYS dictionary.

To add a new domain:
1. Create a new module in this directory (e.g., slack.py)
2. Define your payload classes inheriting from BaseModel
3. Add a ROUTING_KEYS dictionary mapping class names to routing keys
4. The registry will automatically discover and register your events on startup
"""

# Re-export all domain types for convenient access
from event_producers.events.domains.agent.feedback import (
    AgentFeedbackRequested,
    AgentFeedbackResponse,
)
from event_producers.events.domains.agent.thread import (
    AgentThreadPrompt,
    AgentThreadResponse,
    AgentThreadErrorPayload,
)
from event_producers.events.domains.fireflies import (
    SentimentType,
    AIFilters,
    TranscriptSentence,
    MeetingParticipant,
    FirefliesUser,
    FirefliesTranscriptUploadPayload,
    FirefliesTranscriptReadyPayload,
    FirefliesTranscriptProcessedPayload,
    FirefliesTranscriptFailedPayload,
)
from event_producers.events.domains.github import (
    GitHubPRCreatedPayload,
)
from event_producers.events.domains.theboard import (
    MeetingCreatedPayload,
    MeetingStartedPayload,
    RoundCompletedPayload,
    CommentExtractedPayload,
    MeetingConvergedPayload,
    MeetingCompletedPayload,
    MeetingFailedPayload,
    ParticipantAddedPayload,
    ParticipantTurnCompletedPayload,
)

__all__ = [
    # AgentThread domain
    "AgentFeedbackRequested",
    "AgentFeedbackResponse",
    "AgentThreadPrompt",
    "AgentThreadResponse",
    "AgentThreadErrorPayload",
    # Fireflies domain
    "SentimentType",
    "AIFilters",
    "TranscriptSentence",
    "MeetingParticipant",
    "FirefliesUser",
    "FirefliesTranscriptUploadPayload",
    "FirefliesTranscriptReadyPayload",
    "FirefliesTranscriptProcessedPayload",
    "FirefliesTranscriptFailedPayload",
    # GitHub domain
    "GitHubPRCreatedPayload",
    # TheBoard domain
    "MeetingCreatedPayload",
    "MeetingStartedPayload",
    "RoundCompletedPayload",
    "CommentExtractedPayload",
    "MeetingConvergedPayload",
    "MeetingCompletedPayload",
    "MeetingFailedPayload",
    "ParticipantAddedPayload",
    "ParticipantTurnCompletedPayload",
]
