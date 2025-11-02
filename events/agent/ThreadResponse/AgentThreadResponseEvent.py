"""
Agent thread response event.
"""

from typing import Optional
from events.base import BaseEvent


class AgentThreadResponseEvent(BaseEvent):
    """
    Agent responded to prompt.

    Published when: Agent returns response
    Consumed by: Analytics, logging
    Routing Key: agent.thread.response

    Correlation: Links back to prompt event via correlation_ids
    """

    provider: str
    prompt_id: Optional[str] = None  # Deprecated - use correlation_ids instead
    response: str
    model: Optional[str] = None
    tokens_used: Optional[int] = None
    duration_ms: Optional[int] = None

    @classmethod
    def get_routing_key(cls) -> str:
        return "agent.thread.response"
