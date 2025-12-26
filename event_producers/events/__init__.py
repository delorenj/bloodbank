"""
Event producers events package with unified envelope system.

This package contains event payload definitions organized by domain:
- base: Core event envelope types
- domains: Domain-specific event payloads (fireflies, agent_thread, github, llm, artifact, etc.)
- registry: Event type registry for validation and schema introspection
- utils: Utility functions for working with events
- envelope: Unified envelope creation system

The envelope module provides a unified interface for creating event envelopes.
This __init__.py provides backward-compatible re-exports from the new modular structure.
All types that were previously in events.py are now re-exported from their new locations.
"""

# Unified envelope system (NEW)
from .envelope import (
    create_envelope,
    create_source,
    create_agent_context,
    create_http_envelope,
    create_agent_envelope,
    create_scheduled_envelope,
    envelope_for,  # Backward compatibility
)

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
from event_producers.events.domains.agent.thread import (
    AgentThreadPrompt,
    AgentThreadResponse,
    AgentThreadErrorPayload,
)

# LLM domain
from event_producers.events.domains.llm import (
    LLMPrompt,
    LLMResponse,
    LLMErrorPayload,
)

# Artifact domain
from event_producers.events.domains.artifact import (
    Artifact,
    ArtifactIngestionFailedPayload,
)

# GitHub domain
from event_producers.events.domains.github import (
    GitHubPRCreatedPayload,
)

# Utilities (legacy - renamed to avoid conflicts)
from event_producers.events.utils import create_envelope as legacy_create_envelope

# Registry
from event_producers.events.registry import (
    EventDomain,
    EventRegistry,
    get_registry,
    register_event,
)

__all__ = [
    # Unified envelope system (NEW)
    "create_envelope",
    "create_source",
    "create_agent_context",
    "create_http_envelope",
    "create_agent_envelope",
    "create_scheduled_envelope",
    "envelope_for",  # Backward compatibility
    "legacy_create_envelope",  # Old utils version
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
    # LLM types
    "LLMPrompt",
    "LLMResponse",
    "LLMErrorPayload",
    # Artifact types
    "Artifact",
    "ArtifactIngestionFailedPayload",
    # GitHub types
    "GitHubPRCreatedPayload",
    # Registry
    "EventDomain",
    "EventRegistry",
    "get_registry",
    "register_event",
]
