"""Agent domain events."""

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
    AgentOpenClawEventType,
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

__all__ = [
    "AgentFeedbackRequested",
    "AgentFeedbackResponse",
    "AgentThreadPrompt",
    "AgentThreadResponse",
    "AgentThreadErrorPayload",
    # OpenClaw agent events
    "AgentOpenClawEventType",
    "AgentMessageReceived",
    "AgentMessageSent",
    "AgentToolInvoked",
    "AgentToolCompleted",
    "AgentSubagentSpawned",
    "AgentSubagentCompleted",
    "AgentSessionStarted",
    "AgentSessionEnded",
    "AgentTaskAssigned",
    "AgentTaskCompleted",
    "AgentHeartbeat",
    "AgentError",
]
