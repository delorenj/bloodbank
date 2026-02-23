"""
AgentThread event payload definitions.

PARTIALLY GENERATED FROM HOLYFIELDS SCHEMAS.
AgentThreadResponse and AgentThreadErrorPayload are re-exported from Holyfields.
AgentThreadPrompt remains hand-written (it's a BaseCommand with execute() logic).

All events are wrapped in EventEnvelope[T] where T is your payload type.
"""

from pydantic import BaseModel, Field
from typing import Optional, List

from event_producers.events.core.abstraction import BaseCommand, BaseEvent, CommandContext, EventCollector
from event_producers.events.base import create_envelope, Source, TriggerType

# ============================================================================
# Re-export from Holyfields generated models (schema-first)
# ============================================================================

from holyfields.compat import (
    AgentThreadResponse,
    AgentThreadErrorPayload,
)


# ============================================================================
# Hand-written: BaseCommand with execute() — cannot be generated from schema
# ============================================================================

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
        """Execute the agent prompt logic."""
        simulated_response_text = f"Echoing your prompt: {self.prompt}"
        
        response_payload = AgentThreadResponse(
            provider=self.provider,
            prompt_id=str(context.correlation_id),
            response=simulated_response_text,
            model=self.model,
            tokens_used=42,
            duration_ms=100
        )

        source = Source(
            host="localhost",
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

        collector.add(response_envelope)
        return response_payload


ROUTING_KEYS = {
    "AgentThreadPrompt": "agent.thread.prompt",
    "AgentThreadResponse": "agent.thread.response",
    "AgentThreadErrorPayload": "agent.thread.error",
}
