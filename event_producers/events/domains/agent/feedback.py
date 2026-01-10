"""
Agent feedback event payload definitions.

All events are wrapped in EventEnvelope[T] where T is your payload type.
"""

from typing import Optional, Dict, Any, List, Literal
from pydantic import Field

from event_producers.events.core.abstraction import BaseCommand, BaseEvent, CommandContext, EventCollector


class AgentFeedbackResponse(BaseEvent):
    """
    Agent feedback response.

    Published when: Feedback request is processed
    Consumed by: Orchestrators, dashboards, analytics
    Routing Key: agent.feedback.response

    Correlation: Links back to request via correlation_ids
    """

    agent_id: str
    letta_agent_id: Optional[str] = None
    response: str = ""
    status: Literal["ok", "error"] = "ok"
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentFeedbackRequested(BaseCommand[AgentFeedbackResponse]):
    """
    Request feedback from a specific agent.

    Published when: A system needs mid-session feedback
    Consumed by: Agent feedback router service
    Routing Key: agent.feedback.requested
    """

    agent_id: str = Field(..., description="AgentForge registry ID")
    message: str = Field(..., description="Message to send to the agent")
    letta_agent_id: Optional[str] = Field(None, description="Override Letta agent ID")
    context: Optional[Dict[str, Any]] = Field(
        default=None, description="Optional context for the agent"
    )
    tags: List[str] = Field(default_factory=list)

    async def execute(
        self, context: CommandContext, collector: EventCollector
    ) -> AgentFeedbackResponse:
        raise NotImplementedError(
            "Agent feedback requests are handled by the agent-feedback-router service."
        )


ROUTING_KEYS = {
    "AgentFeedbackRequested": "agent.feedback.requested",
    "AgentFeedbackResponse": "agent.feedback.response",
}
