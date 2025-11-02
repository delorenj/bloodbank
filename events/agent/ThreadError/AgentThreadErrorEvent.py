"""
Agent thread error event.
"""

from typing import Optional
from events.base import BaseEvent


class AgentThreadErrorEvent(BaseEvent):
    """
    Agent interaction failed.

    Published when: Agent call fails (rate limit, error, timeout)
    Consumed by: Alerting, retry logic
    Routing Key: agent.thread.error

    Correlation: Links back to prompt event via correlation_ids
    """

    provider: str
    model: Optional[str] = None
    error_message: str
    error_code: Optional[str] = None
    is_retryable: bool = False
    retry_count: int = 0

    @classmethod
    def get_routing_key(cls) -> str:
        return "agent.thread.error"
