"""
LLM event payload definitions.

All events are wrapped in EventEnvelope[T] where T is your payload type.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


class LLMPrompt(BaseModel):
    """
    LLM interaction started.
    
    Published when: User sends prompt to LLM
    Consumed by: Analytics, logging, prompt caching
    Routing Key: llm.prompt
    """
    provider: str  # e.g., "anthropic", "openai", "google"
    model: Optional[str] = None  # e.g., "claude-sonnet-4", "gpt-4"
    prompt: str
    project: Optional[str] = None  # Git project name
    working_dir: Optional[str] = None
    domain: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class LLMResponse(BaseModel):
    """
    LLM responded to prompt.
    
    Published when: LLM returns response
    Consumed by: Analytics, logging
    Routing Key: llm.response
    
    Correlation: Links back to prompt event via correlation_ids
    """
    provider: str
    prompt_id: Optional[str] = None  # Deprecated - use correlation_ids instead
    response: str
    model: Optional[str] = None
    tokens_used: Optional[int] = None
    duration_ms: Optional[int] = None


class LLMErrorPayload(BaseModel):
    """
    LLM interaction failed.
    
    Published when: LLM call fails (rate limit, error, timeout)
    Consumed by: Alerting, retry logic
    Routing Key: llm.error
    
    Correlation: Links back to prompt event via correlation_ids
    """
    provider: str
    model: Optional[str] = None
    error_message: str
    error_code: Optional[str] = None
    is_retryable: bool = False
    retry_count: int = 0


ROUTING_KEYS = {
    "LLMPrompt": "llm.prompt",
    "LLMResponse": "llm.response",
    "LLMErrorPayload": "llm.error",
}