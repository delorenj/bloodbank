"""
Artifact event payload definitions.

All events are wrapped in EventEnvelope[T] where T is your payload type.
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, Literal


class Artifact(BaseModel):
    """
    Artifact lifecycle event.

    Published when: Artifact is created, updated, or deleted
    Consumed by: File indexer, version control, RAG system
    Routing Key: artifact.{action} (e.g., artifact.created, artifact.updated)
    """

    action: Literal["created", "updated", "deleted"]
    kind: str  # e.g., "transcript", "code", "document"
    uri: str  # File path or URL
    title: Optional[str] = None
    content: Optional[str] = None  # Optional full content
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ArtifactIngestionFailedPayload(BaseModel):
    """
    Artifact ingestion into RAG failed.

    Published when: RAG ingestion fails for any artifact
    Consumed by: Alerting, retry service
    Routing Key: artifact.ingestion.failed

    Correlation: Links back to artifact.created/updated event
    """

    artifact_uri: str
    artifact_kind: str
    error_message: str
    error_code: Optional[str] = None
    retry_count: int = 0
    is_retryable: bool = True


ROUTING_KEYS = {
    "Artifact": "artifact.{action}",  # Action will be interpolated
    "ArtifactIngestionFailedPayload": "artifact.ingestion.failed",
}
