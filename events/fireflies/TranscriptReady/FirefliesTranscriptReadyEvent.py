"""
Fireflies transcript ready event.
"""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Dict, Any
from events.base import BaseEvent
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


class FirefliesTranscriptReadyEvent(BaseEvent):
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

    @classmethod
    def get_routing_key(cls) -> str:
        return "fireflies.transcript.ready"
