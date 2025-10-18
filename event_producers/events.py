"""
Event payload definitions for Bloodbank event bus.

All events are wrapped in EventEnvelope[T] where T is your payload type.
"""

from pydantic import BaseModel, Field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Generic, TypeVar, Literal
from uuid import UUID, uuid4
from enum import Enum


# ============================================================================
# Core Envelope Types
# ============================================================================


class TriggerType(str, Enum):
    """How was this event triggered?"""

    MANUAL = "manual"  # Human-initiated
    AGENT = "agent"  # AI agent triggered
    SCHEDULED = "scheduled"  # Cron/timer triggered
    FILE_WATCH = "file_watch"  # File system event
    HOOK = "hook"  # External webhook


class Source(BaseModel):
    """Identifies WHO or WHAT triggered the event."""

    host: str  # Machine that generated event
    type: TriggerType  # How was this triggered?
    app: Optional[str] = None  # Application name
    meta: Optional[Dict[str, Any]] = None  # Additional context


class AgentType(str, Enum):
    """Known agent types in the 33GOD ecosystem."""

    CLAUDE_CODE = "claude-code"
    CLAUDE_CHAT = "claude-chat"
    GEMINI_CLI = "gemini-cli"
    GEMINI_CODE = "gemini-code"
    LETTA = "letta"
    AGNO = "agno"
    SMOLAGENT = "smolagent"
    ATOMIC_AGENT = "atomic-agent"
    CUSTOM = "custom"


class CodeState(BaseModel):
    """Git context for agent's working environment."""

    repo_url: Optional[str] = None
    branch: Optional[str] = None
    working_diff: Optional[str] = None  # Unstaged changes
    branch_diff: Optional[str] = None  # Diff vs main
    last_commit_hash: Optional[str] = None


class AgentContext(BaseModel):
    """Rich metadata about the AI agent (when source.type == AGENT)."""

    type: AgentType
    name: Optional[str] = None  # Agent's persona/name
    system_prompt: Optional[str] = None  # Initial system prompt
    instance_id: Optional[str] = None  # Unique session identifier
    mcp_servers: Optional[List[str]] = None  # Connected MCP servers
    file_references: Optional[List[str]] = None  # Files in context
    url_references: Optional[List[str]] = None  # URLs in context
    code_state: Optional[CodeState] = None  # Git state snapshot
    checkpoint_id: Optional[str] = None  # For checkpoint-based agents
    meta: Optional[Dict[str, Any]] = None  # Extensibility


T = TypeVar("T")


class EventEnvelope(BaseModel, Generic[T]):
    """
    Generic event envelope that wraps all events.

    Versioning Strategy:
    - Bump 'version' field for breaking changes to envelope structure
    - Payload schemas can evolve independently (add optional fields)
    - For breaking payload changes, create new event type (e.g., .v2)
    """

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str  # Routing key (e.g., "fireflies.transcript.ready")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0.0"  # Envelope schema version
    source: Source  # Who/what triggered this
    correlation_ids: List[UUID] = Field(default_factory=list)  # Parent event IDs
    agent_context: Optional[AgentContext] = None  # Agent metadata (if applicable)
    payload: T  # Your typed event data

    class Config:
        json_encoders = {UUID: str, datetime: lambda v: v.isoformat()}


# ============================================================================
# Fireflies Events
# ============================================================================


class SentimentType(str, Enum):
    """Sentiment classification for transcript sentences."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class AIFilters(BaseModel):
    """AI-extracted metadata from a sentence."""

    text_cleanup: str
    task: Optional[str] = None
    pricing: Optional[str] = None
    metric: Optional[str] = None
    question: Optional[str] = None
    date_and_time: Optional[str] = None
    sentiment: SentimentType


class TranscriptSentence(BaseModel):
    """A single sentence from the transcript."""

    index: int
    speaker_name: Optional[str] = None
    speaker_id: int
    raw_text: str
    text: str  # Cleaned version
    start_time: float  # Seconds from start
    end_time: float  # Seconds from start
    ai_filters: AIFilters


class MeetingParticipant(BaseModel):
    """A participant in the meeting."""

    name: str
    email: Optional[str] = None


class FirefliesUser(BaseModel):
    """Fireflies user who owns the transcript."""

    user_id: str
    email: str
    name: str
    num_transcripts: int
    minutes_consumed: float
    is_admin: bool


class FirefliesTranscriptUploadPayload(BaseModel):
    """
    Request to upload media to Fireflies for transcription.

    Published when: File watcher detects new recording, or manual upload
    Consumed by: n8n workflow that uploads to Fireflies API
    Routing Key: fireflies.transcript.upload

    Generate deterministic event_id using:
        tracker.generate_event_id(
            "fireflies.transcript.upload",
            unique_key=f"{user_id}|{file_path}"
        )
    """

    media_file: str  # Path or URL to media file
    media_duration_seconds: int
    media_type: str  # e.g., "audio/mpeg", "video/mp4"
    title: Optional[str] = None  # Meeting title
    user_id: Optional[str] = None  # User requesting transcription
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FirefliesTranscriptReadyPayload(BaseModel):
    """
    Published when Fireflies completes transcription.

    This is the webhook payload we receive from Fireflies.
    Contains FULL transcript data - consumers don't need additional API calls.

    Published when: Fireflies webhook fires
    Consumed by: RAG ingestion service, notification service
    Routing Key: fireflies.transcript.ready

    Correlation: Links back to upload event via correlation_ids
    """

    # Core identifiers
    id: str = Field(..., description="Fireflies meeting/transcript ID")
    title: str
    date: datetime  # Meeting date/time
    duration: float  # Duration in minutes

    # URLs
    transcript_url: str
    audio_url: Optional[str] = None
    video_url: Optional[str] = None

    # Content (full transcript included!)
    sentences: List[TranscriptSentence]
    summary: Optional[str] = None  # May be None if not generated yet

    # Participants
    participants: List[MeetingParticipant] = Field(default_factory=list)
    speakers: List[str] = Field(default_factory=list)  # Unique speaker names

    # Metadata
    user: FirefliesUser
    host_email: str
    organizer_email: str
    privacy: str  # e.g., "link", "private"

    # Meeting info
    meeting_link: Optional[str] = None
    calendar_id: Optional[str] = None
    calendar_type: Optional[str] = None

    # Raw data for extensibility
    raw_meeting_info: Optional[Dict[str, Any]] = Field(
        default=None, description="Full meeting_info object from webhook"
    )


class FirefliesTranscriptProcessedPayload(BaseModel):
    """
    Published after transcript is ingested into RAG system.

    Published when: RAG consumer finishes processing
    Consumed by: Notification service, analytics
    Routing Key: fireflies.transcript.processed

    Correlation: Links back to ready event via correlation_ids
    """

    transcript_id: str  # Fireflies ID
    rag_document_id: str  # Our internal RAG document ID
    title: str
    ingestion_timestamp: datetime
    sentence_count: int
    speaker_count: int
    duration_minutes: float
    vector_store: str  # e.g., "chroma", "pinecone"
    chunk_count: int = 0  # Number of chunks created
    embedding_model: Optional[str] = None  # Model used for embeddings
    metadata: Dict[str, Any] = Field(default_factory=dict)


class FirefliesTranscriptFailedPayload(BaseModel):
    """
    Published when transcription or processing fails.

    Published when: Upload fails, transcription fails, or RAG ingestion fails
    Consumed by: Alerting service, monitoring
    Routing Key: fireflies.transcript.failed

    Correlation: Links back to the failed event via correlation_ids
    """

    failed_stage: Literal["upload", "transcription", "processing"]
    error_message: str
    error_code: Optional[str] = None
    transcript_id: Optional[str] = None  # May not have ID if upload failed
    media_file: Optional[str] = None  # Original file path/URL
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    retry_count: int = 0
    is_retryable: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# AgentThread Events
# ============================================================================


class AgentThreadPrompt(BaseModel):
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


class AgentThreadResponse(BaseModel):
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


class AgentThreadErrorPayload(BaseModel):
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


# ============================================================================
# Helper Functions
# ============================================================================


def create_envelope(
    event_type: str,
    payload: Any,
    source: Source,
    correlation_ids: Optional[List[UUID]] = None,
    agent_context: Optional[AgentContext] = None,
    event_id: Optional[UUID] = None,
) -> EventEnvelope:
    """
    Helper to create properly-formed event envelope.

    Args:
        event_type: Routing key (e.g., "fireflies.transcript.ready")
        payload: Your typed payload
        source: Source metadata
        correlation_ids: List of parent event IDs (for causation tracking)
        agent_context: Agent metadata (if source.type == AGENT)
        event_id: Optional explicit event ID (for deterministic IDs)

    Returns:
        EventEnvelope with proper typing
    """
    return EventEnvelope(
        event_id=event_id or uuid4(),
        event_type=event_type,
        timestamp=datetime.now(timezone.utc),
        version="1.0.0",
        source=source,
        correlation_ids=correlation_ids or [],
        agent_context=agent_context,
        payload=payload,
    )
