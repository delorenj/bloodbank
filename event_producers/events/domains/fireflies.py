"""
Fireflies event payload definitions.

All events are wrapped in EventEnvelope[T] where T is your payload type.
"""

from pydantic import BaseModel, Field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Literal
from enum import Enum


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


ROUTING_KEYS = {
    "FirefliesTranscriptUploadPayload": "fireflies.transcript.upload",
    "FirefliesTranscriptReadyPayload": "fireflies.transcript.ready",
    "FirefliesTranscriptProcessedPayload": "fireflies.transcript.processed",
    "FirefliesTranscriptFailedPayload": "fireflies.transcript.failed",
}
