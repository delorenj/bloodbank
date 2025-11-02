"""
Fireflies transcript processed event.
"""

from pydantic import Field
from datetime import datetime
from typing import Dict, Any, Optional
from events.base import BaseEvent


class FirefliesTranscriptProcessedEvent(BaseEvent):
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

    @classmethod
    def get_routing_key(cls) -> str:
        return "fireflies.transcript.processed"
