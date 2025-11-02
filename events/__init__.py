"""
Bloodbank event definitions.

All events are organized by domain and derive from BaseEvent or Command.
"""

from .base import BaseEvent, Command
from .core import EventEnvelope, TriggerType, Source, AgentContext, AgentType, CodeState
from .fireflies import (
    FirefliesTranscriptUploadEvent,
    FirefliesTranscriptReadyEvent,
    FirefliesTranscriptProcessedEvent,
    FirefliesTranscriptFailedEvent,
)
from .agent import (
    AgentThreadPromptEvent,
    AgentThreadResponseEvent,
    AgentThreadErrorEvent,
)

__all__ = [
    # Base classes
    "BaseEvent",
    "Command",
    # Core types
    "EventEnvelope",
    "TriggerType",
    "Source",
    "AgentContext",
    "AgentType",
    "CodeState",
    # Fireflies events
    "FirefliesTranscriptUploadEvent",
    "FirefliesTranscriptReadyEvent",
    "FirefliesTranscriptProcessedEvent",
    "FirefliesTranscriptFailedEvent",
    # Agent events
    "AgentThreadPromptEvent",
    "AgentThreadResponseEvent",
    "AgentThreadErrorEvent",
]
