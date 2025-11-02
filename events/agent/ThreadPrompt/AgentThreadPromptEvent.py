"""
Agent thread prompt event.
"""

from pydantic import Field
from typing import Optional, List
from events.base import BaseEvent


class AgentThreadPromptEvent(BaseEvent):
    """
    A prompt is sent to an agent.

    Published when: User sends prompt to AgentThread
    Consumed by: Analytics, logging, prompt caching
    Routing Key: agent.thread.prompt
    """

    provider: str  # e.g., "anthropic", "openai", "google"
    model: Optional[str] = None  # e.g., "claude-sonnet-4", "gpt-4"
    prompt: str
    project: Optional[str] = None  # Git project name
    working_dir: Optional[str] = None
    domain: Optional[str] = None
    tags: List[str] = Field(default_factory=list)

    @classmethod
    def get_routing_key(cls) -> str:
        return "agent.thread.prompt"
