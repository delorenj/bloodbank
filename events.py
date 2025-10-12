from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List, Literal
from datetime import datetime, timezone
import uuid


class EventEnvelope(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: str
    source: str  # e.g., "cli/claude", "http/fireflies", "vscode", "n8n"
    project: Optional[str] = None  # git repo name, or logical project
    working_dir: Optional[str] = None  # cwd when prompt happened
    domain: Optional[str] = None  # “general domain” fallback
    correlation_id: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    data: Dict[str, Any]  # holds the typed payload


# --- Payloads ---


class LLMPrompt(BaseModel):
    provider: Literal[
        "anthropic",
        "openai",
        "google",
        "meta",
        "microsoft",
        "crush",
        "auggie",
        "copilot",
        "opencode",
        "gptme",
        "codex",
        "other",
    ]
    model: Optional[str] = None
    prompt: str
    temperature: Optional[float] = None
    tools: Optional[List[str]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    provider: str
    model: Optional[str] = None
    prompt_id: str  # correlation to original prompt event id
    response: str
    usage: Optional[Dict[str, Any]] = None  # token counts, latency, etc.
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Artifact(BaseModel):
    action: Literal["created", "updated"]
    kind: Literal["file", "transcript", "image", "notebook", "dataset", "other"]
    uri: str  # file path or URL
    title: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class CalendarEvent(BaseModel):
    summary: str
    start_time: datetime
    end_time: datetime
    location: Optional[str] = None


def envelope_for(
    event_type: str, source: str, data: BaseModel, **kwargs
) -> EventEnvelope:
    return EventEnvelope(
        event_type=event_type, source=source, data=data.model_dump(), **kwargs
    )