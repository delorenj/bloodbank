"""
Event producers events package.

This package contains event payload definitions organized by domain:
- base: Core event envelope types
- domains: Domain-specific event payloads (fireflies, agent_thread, github, etc.)
- registry: Event type registry for validation and schema introspection
- utils: Utility functions for working with events

This __init__.py provides backward-compatible re-exports from the new modular structure.
All types that were previously in events.py are now re-exported from their new locations.
"""

# Base types
from event_producers.events.base import (
    EventEnvelope,
    TriggerType,
    Source,
    AgentType,
    AgentContext,
    CodeState,
)

# Fireflies domain
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

# AgentThread domain
from event_producers.events.domains.agent_thread import (
    AgentThreadPrompt,
    AgentThreadResponse,
    AgentThreadErrorPayload,
)

# GitHub domain
from event_producers.events.domains.github import (
    GitHubPRCreatedPayload,
)

# Utilities
from event_producers.events.utils import create_envelope

# Registry
from event_producers.events.registry import (
    EventDomain,
    EventRegistry,
    get_registry,
    register_event,
)

__all__ = [
    # Base types
    "EventEnvelope",
    "TriggerType",
    "Source",
    "AgentType",
    "AgentContext",
    "CodeState",
    # Fireflies types
    "SentimentType",
    "AIFilters",
    "TranscriptSentence",
    "MeetingParticipant",
    "FirefliesUser",
    "FirefliesTranscriptUploadPayload",
    "FirefliesTranscriptReadyPayload",
    "FirefliesTranscriptProcessedPayload",
    "FirefliesTranscriptFailedPayload",
    # AgentThread types
    "AgentThreadPrompt",
    "AgentThreadResponse",
    "AgentThreadErrorPayload",
    # GitHub types
    "GitHubPRCreatedPayload",
    # Utilities
    "create_envelope",
    # Registry
    "EventDomain",
    "EventRegistry",
    "get_registry",
    "register_event",
]
