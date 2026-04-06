"""Agent feedback event payload definitions.

These classes remain schema-first via Holyfields, but Bloodbank wraps them in
``BaseEvent`` so legacy discovery and ADR-0002 tests continue to treat them as
pure event payloads rather than plain Pydantic models.
"""

from typing import Literal

from event_producers.events.core.abstraction import BaseEvent
from holyfields.compat import (
    AgentFeedbackRequested as HolyfieldsAgentFeedbackRequested,
    AgentFeedbackResponse as HolyfieldsAgentFeedbackResponse,
)


class AgentFeedbackRequested(HolyfieldsAgentFeedbackRequested, BaseEvent):
    """Bloodbank-compatible wrapper for the Holyfields feedback request model."""


class AgentFeedbackResponse(HolyfieldsAgentFeedbackResponse, BaseEvent):
    """Bloodbank-compatible wrapper for the Holyfields feedback response model."""

    status: Literal["ok", "error"] = "ok"


ROUTING_KEYS = {
    "AgentFeedbackRequested": "agent.feedback.requested",
    "AgentFeedbackResponse": "agent.feedback.response",
}
