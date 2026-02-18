"""Pydantic event schemas for the Bloodbank heartbeat system."""

from pydantic import BaseModel, Field
from typing import Optional, Dict, Any


class HeartbeatPromptPayload(BaseModel):
    """Payload for prompt-based heartbeat events routed to agents."""

    sink: str = Field(..., description="Target: 'global' or agent name")
    description: str = Field(..., description="Human-readable description of this heartbeat")
    prompt: str = Field(..., description="Prompt text to deliver to the agent")
    schedule_key: str = Field(..., description="HHMM slot that triggered this")
    idempotency_key: str = Field(..., description="YYYYmmdd_HHMM key for dedup")


class HeartbeatCommandPayload(BaseModel):
    """Payload for command-based heartbeat events (run binary directly)."""

    sink: str = Field(..., description="Target: 'global' or agent name")
    description: str = Field(..., description="Human-readable description")
    command: str = Field(..., description="Binary to execute")
    args: list[str] = Field(default_factory=list, description="Command arguments")
    schedule_key: str = Field(..., description="HHMM slot that triggered this")
    idempotency_key: str = Field(..., description="YYYYmmdd_HHMM key for dedup")
