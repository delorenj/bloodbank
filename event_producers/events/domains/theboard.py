"""TheBoard meeting event domain.

Events emitted by theboard multi-agent meeting orchestration system.

Event Hierarchy:
- theboard.meeting.created - New meeting initialized
- theboard.meeting.started - Meeting began execution
- theboard.meeting.round_completed - Round finished
- theboard.meeting.comment_extracted - Comment/idea extracted from response
- theboard.meeting.converged - Meeting reached convergence
- theboard.meeting.completed - Meeting finished successfully
- theboard.meeting.failed - Meeting execution failed

Future Extensions:
- theboard.meeting.participant.added
- theboard.meeting.participant.turn.completed
- theboard.meeting.artifact.created (via 33god artifact command)
"""

from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import Field

from event_producers.events.core.abstraction import BaseEvent


# Event Payloads (Data Models)

class MeetingCreatedPayload(BaseEvent):
    """Emitted when a new meeting is created."""
    meeting_id: UUID
    topic: str
    strategy: str  # 'sequential' or 'greedy'
    max_rounds: int
    agent_count: Optional[int] = None


class MeetingStartedPayload(BaseEvent):
    """Emitted when a meeting transitions to RUNNING status."""
    meeting_id: UUID
    selected_agents: list[str]  # Agent names
    agent_count: int


class RoundCompletedPayload(BaseEvent):
    """Emitted when a meeting round completes."""
    meeting_id: UUID
    round_num: int
    agent_name: str
    response_length: int
    comment_count: int
    avg_novelty: float
    tokens_used: int
    cost: float


class CommentExtractedPayload(BaseEvent):
    """Emitted when comments are extracted from agent response."""
    meeting_id: UUID
    round_num: int
    agent_name: str
    comment_text: str
    category: str  # 'technical_decision', 'risk', 'implementation_detail', etc.
    novelty_score: float


class MeetingConvergedPayload(BaseEvent):
    """Emitted when meeting reaches convergence."""
    meeting_id: UUID
    round_num: int
    avg_novelty: float
    novelty_threshold: float
    total_comments: int


class MeetingCompletedPayload(BaseEvent):
    """Emitted when meeting completes successfully."""
    meeting_id: UUID
    total_rounds: int
    total_comments: int
    total_cost: float
    convergence_detected: bool
    stopping_reason: str


class MeetingFailedPayload(BaseEvent):
    """Emitted when meeting execution fails."""
    meeting_id: UUID
    error_type: str
    error_message: str
    round_num: Optional[int] = None
    agent_name: Optional[str] = None


# Future Events (Placeholders for theboardroom integration)

class ParticipantAddedPayload(BaseEvent):
    """Emitted when agent added to meeting (future)."""
    meeting_id: UUID
    agent_id: UUID
    agent_name: str
    expertise: str


class ParticipantTurnCompletedPayload(BaseEvent):
    """Emitted when participant completes their turn (future)."""
    meeting_id: UUID
    round_num: int
    agent_id: UUID
    agent_name: str
    turn_type: str  # 'turn' or 'response'
    response_text: str
    tokens_used: int


# Routing Keys Mapping
# Maps event types to payload classes for registry auto-discovery

ROUTING_KEYS = {
    "theboard.meeting.created": MeetingCreatedPayload,
    "theboard.meeting.started": MeetingStartedPayload,
    "theboard.meeting.round_completed": RoundCompletedPayload,
    "theboard.meeting.comment_extracted": CommentExtractedPayload,
    "theboard.meeting.converged": MeetingConvergedPayload,
    "theboard.meeting.completed": MeetingCompletedPayload,
    "theboard.meeting.failed": MeetingFailedPayload,
    # Future events
    "theboard.meeting.participant.added": ParticipantAddedPayload,
    "theboard.meeting.participant.turn.completed": ParticipantTurnCompletedPayload,
}
