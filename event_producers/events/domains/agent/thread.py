"""
AgentThread event payload definitions.

All events are wrapped in EventEnvelope[T] where T is your payload type.
"""

from pydantic import BaseModel, Field
from typing import Optional, List
import asyncio

from event_producers.events.core.abstraction import BaseCommand, BaseEvent, CommandContext, EventCollector
from event_producers.events.base import create_envelope, Source, TriggerType


class AgentThreadResponse(BaseEvent):
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


class AgentThreadErrorPayload(BaseEvent):
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


class AgentThreadPrompt(BaseCommand[AgentThreadResponse]):
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

    async def execute(self, context: CommandContext, collector: EventCollector) -> AgentThreadResponse:
        """
        Execute the agent prompt logic.
        """
        # SIMULATION: In a real system, this would call the LLM Provider
        simulated_response_text = f"Echoing your prompt: {self.prompt}"
        
        response_payload = AgentThreadResponse(
            provider=self.provider,
            prompt_id=str(context.correlation_id), # Legacy field
            response=simulated_response_text,
            model=self.model,
            tokens_used=42,
            duration_ms=100
        )

        # Create Side Effect Event
        source = Source(
            host="localhost", # Should be dynamic or passed in context
            type=TriggerType.AGENT,
            app="bloodbank-command-executor"
        )

        response_envelope = create_envelope(
            event_type="agent.thread.response",
            payload=response_payload,
            source=source,
            correlation_ids=[context.correlation_id],
            agent_context=context.agent_context
        )

        # Register side effect
        collector.add(response_envelope)

        return response_payload


ROUTING_KEYS = {
    "AgentThreadPrompt": "agent.thread.prompt",
    "AgentThreadResponse": "agent.thread.response",
    "AgentThreadErrorPayload": "agent.thread.error",
}