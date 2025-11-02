"""
Fireflies transcript failed event.
"""

from pydantic import Field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Literal
from events.base import BaseEvent


class FirefliesTranscriptFailedEvent(BaseEvent):
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

    @classmethod
    def get_routing_key(cls) -> str:
        return "fireflies.transcript.failed"
