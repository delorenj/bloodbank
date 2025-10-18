"""
Proposed Fireflies Event Schemas for Bloodbank
Based on actual webhook payload analysis
"""
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Dict, Any, Literal
from enum import Enum


# ============================================================================
# Shared Types
# ============================================================================

class SentimentType(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class AIFilters(BaseModel):
    """AI-extracted metadata from a sentence"""
    text_cleanup: str
    task: Optional[str] = None
    pricing: Optional[str] = None
    metric: Optional[str] = None
    question: Optional[str] = None
    date_and_time: Optional[str] = None
    sentiment: SentimentType


class TranscriptSentence(BaseModel):
    """A single sentence from the transcript"""
    index: int
    speaker_name: Optional[str] = None
    speaker_id: int
    raw_text: str
    text: str  # Cleaned version
    start_time: float  # seconds
    end_time: float  # seconds
    ai_filters: AIFilters


class MeetingParticipant(BaseModel):
    """A participant in the meeting"""
    name: str
    email: Optional[str] = None


class FirefliesUser(BaseModel):
    """Fireflies user who owns the transcript"""
    user_id: str
    email: str
    name: str
    num_transcripts: int
    minutes_consumed: float
    is_admin: bool


# ============================================================================
# Event Payloads
# ============================================================================

class FirefliesTranscriptUploadPayload(BaseModel):
    """
    Published when we need to upload media to Fireflies for transcription.
    Typically triggered by file_watch or manual upload.
    
    Routing Key: fireflies.transcript.upload
    """
    media_file: str  # Path or URL to the media file
    media_duration_seconds: int
    media_type: str  # e.g., "audio/mpeg", "video/mp4"
    title: Optional[str] = None  # Optional meeting title
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FirefliesTranscriptReadyPayload(BaseModel):
    """
    Published when Fireflies completes transcription.
    This is the webhook payload we receive from Fireflies.
    
    Routing Key: fireflies.transcript.ready
    
    Note: This contains the FULL transcript data, not just a meeting_id.
    Consumers can immediately process without additional API calls.
    """
    # Core identifiers
    id: str = Field(..., description="Fireflies meeting/transcript ID")
    title: str
    date: datetime  # Unix timestamp in webhook, converted to datetime
    duration: float  # In minutes
    
    # URLs
    transcript_url: str
    audio_url: Optional[str] = None
    video_url: Optional[str] = None
    
    # Content
    sentences: List[TranscriptSentence]
    summary: Optional[str] = None  # May be None if summary not generated
    
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
        default=None, 
        description="Full meeting_info object from webhook"
    )


class FirefliesTranscriptProcessedPayload(BaseModel):
    """
    Published after we've ingested the transcript into our RAG system.
    This signals downstream consumers that the transcript is searchable.
    
    Routing Key: fireflies.transcript.processed
    """
    transcript_id: str  # Fireflies ID
    rag_document_id: str  # Our internal RAG document ID
    title: str
    ingestion_timestamp: datetime
    sentence_count: int
    speaker_count: int
    duration_minutes: float
    vector_store: str  # e.g., "chroma", "pinecone", etc.
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Example Usage
# ============================================================================

def example_upload_event():
    """Example: Triggering a transcription upload"""
    from uuid import uuid4
    
    # This would be wrapped in EventEnvelope[FirefliesTranscriptUploadPayload]
    payload = FirefliesTranscriptUploadPayload(
        media_file="/mnt/recordings/daily-standup-2025-10-18.mp3",
        media_duration_seconds=900,  # 15 minutes
        media_type="audio/mpeg",
        title="Daily Standup - Oct 18"
    )
    
    # The event_id from this envelope would be used as correlation_id
    # in the subsequent transcript.ready event
    return payload


def example_ready_event():
    """Example: Parsing Fireflies webhook into our schema"""
    
    # Simplified - actual webhook has more fields
    webhook_data = {
        "id": "01K7CKT5XHEY6DP8BH4CND1QKZ",
        "title": "Daily Standup - Oct 18",
        "date": 1760286300000,  # Unix timestamp
        "duration": 15.5,
        "transcript_url": "https://app.fireflies.ai/view/01K7CKT5...",
        "sentences": [...],  # Array of sentence objects
        "user": {...},  # User object
        # ... etc
    }
    
    # Transform to our payload
    payload = FirefliesTranscriptReadyPayload(
        id=webhook_data["id"],
        title=webhook_data["title"],
        date=datetime.fromtimestamp(webhook_data["date"] / 1000),
        duration=webhook_data["duration"],
        # ... map remaining fields
    )
    
    return payload


def example_processed_event():
    """Example: After RAG ingestion completes"""
    
    payload = FirefliesTranscriptProcessedPayload(
        transcript_id="01K7CKT5XHEY6DP8BH4CND1QKZ",
        rag_document_id="rag_doc_789xyz",
        title="Daily Standup - Oct 18",
        ingestion_timestamp=datetime.utcnow(),
        sentence_count=42,
        speaker_count=4,
        duration_minutes=15.5,
        vector_store="chroma",
        metadata={
            "tags": ["standup", "engineering"],
            "project": "33GOD",
            "embeddings_model": "text-embedding-3-large"
        }
    )
    
    return payload


# ============================================================================
# Event Flow Example
# ============================================================================

"""
Complete Fireflies event flow:

1. File watcher detects new recording
   → Publish: fireflies.transcript.upload
   → Payload: FirefliesTranscriptUploadPayload
   → event_id: abc-123

2. n8n workflow uploads to Fireflies API
   → Fireflies processes asynchronously

3. Fireflies webhook fires when complete
   → Publish: fireflies.transcript.ready
   → Payload: FirefliesTranscriptReadyPayload (full transcript)
   → correlation_id: abc-123 (links back to upload event)
   → event_id: def-456

4. RAG consumer ingests transcript
   → Publish: fireflies.transcript.processed
   → Payload: FirefliesTranscriptProcessedPayload
   → correlation_id: def-456 (links back to ready event)

This creates a traceable chain:
upload (abc-123) → ready (def-456, corr: abc-123) → processed (ghi-789, corr: def-456)
"""
