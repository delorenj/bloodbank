"""
Fireflies transcript upload request event.
"""

from pydantic import Field
from datetime import datetime, timezone
from typing import Optional
from events.base import BaseEvent


class FirefliesTranscriptUploadEvent(BaseEvent):
    """
    Request to upload media to Fireflies for transcription.

    Published when: File watcher detects new recording, or manual upload
    Consumed by: n8n workflow that uploads to Fireflies API
    Routing Key: fireflies.transcript.upload

    Generate deterministic event_id using:
        tracker.generate_event_id(
            "fireflies.transcript.upload",
            unique_key=f"{user_id}|{media_file}"
        )
    """

    media_file: str  # Path or URL to media file
    media_duration_seconds: int
    media_type: str  # e.g., "audio/mpeg", "video/mp4"
    title: Optional[str] = None  # Meeting title
    user_id: Optional[str] = None  # User requesting transcription
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def get_routing_key(cls) -> str:
        return "fireflies.transcript.upload"
