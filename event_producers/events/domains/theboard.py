"""TheBoard meeting event domain.

GENERATED FROM HOLYFIELDS SCHEMAS — Do not edit manually.
To update: modify JSON schemas in holyfields/schemas/theboard/, regenerate, re-export here.

Events emitted by theboard multi-agent meeting orchestration system.

Event Hierarchy:
- theboard.meeting.created - New meeting initialized
- theboard.meeting.started - Meeting began execution
- theboard.meeting.round_completed - Round finished
- theboard.meeting.comment_extracted - Comment/idea extracted from response
- theboard.meeting.converged - Meeting reached convergence
- theboard.meeting.completed - Meeting finished successfully
- theboard.meeting.failed - Meeting execution failed
"""

from typing import Optional
from uuid import UUID

from pydantic import Field

from event_producers.events.core.abstraction import BaseEvent

# ============================================================================
# Re-export from Holyfields generated models (schema-first)
# ============================================================================

from holyfields.compat import (
    CommentExtractedPayload,
    MeetingCompletedPayload,
    MeetingConvergedPayload,
    MeetingCreatedPayload,
    MeetingFailedPayload,
    MeetingStartedPayload,
    RoundCompletedPayload,
)


# ============================================================================
# Future Events (no Holyfields schema yet — hand-written placeholders)
# ============================================================================

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


# ============================================================================
# Routing Keys Mapping
# ============================================================================

ROUTING_KEYS = {
    "theboard.meeting.created": MeetingCreatedPayload,
    "theboard.meeting.started": MeetingStartedPayload,
    "theboard.meeting.round_completed": RoundCompletedPayload,
    "theboard.meeting.comment_extracted": CommentExtractedPayload,
    "theboard.meeting.converged": MeetingConvergedPayload,
    "theboard.meeting.completed": MeetingCompletedPayload,
    "theboard.meeting.failed": MeetingFailedPayload,
    # Future events (hand-written, no schema yet)
    "theboard.meeting.participant.added": ParticipantAddedPayload,
    "theboard.meeting.participant.turn.completed": ParticipantTurnCompletedPayload,
}
