"""
Agent feedback event payload definitions.

GENERATED FROM HOLYFIELDS SCHEMAS — Do not edit manually.
To update: modify JSON schemas in holyfields/schemas/agent/feedback/, regenerate, re-export here.

All events are wrapped in EventEnvelope[T] where T is your payload type.
"""

# ============================================================================
# Re-export from Holyfields generated models (schema-first)
# ============================================================================

from holyfields.compat import (
    AgentFeedbackRequested,
    AgentFeedbackResponse,
)


ROUTING_KEYS = {
    "AgentFeedbackRequested": "agent.feedback.requested",
    "AgentFeedbackResponse": "agent.feedback.response",
}
