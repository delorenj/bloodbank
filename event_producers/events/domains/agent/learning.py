"""Agent learning event payload definitions.

Bloodbank keeps Holyfields as the schema source of truth, then wraps the
generated models in ``BaseEvent`` so event discovery and legacy typing continue
to behave like the rest of the event domain modules.
"""

from typing import Literal

from event_producers.events.core.abstraction import BaseEvent
from holyfields.compat import (
    AgentLearningCandidateExtracted as HolyfieldsAgentLearningCandidateExtracted,
    AgentLearningCandidateValidated as HolyfieldsAgentLearningCandidateValidated,
    AgentLearningEpisodeCreated as HolyfieldsAgentLearningEpisodeCreated,
    AgentLearningLessonPromoted as HolyfieldsAgentLearningLessonPromoted,
    AgentLearningLessonRejected as HolyfieldsAgentLearningLessonRejected,
    AgentLearningLessonRolledBack as HolyfieldsAgentLearningLessonRolledBack,
    AgentLearningObservationRecorded as HolyfieldsAgentLearningObservationRecorded,
    AgentLearningRetrievalApplied as HolyfieldsAgentLearningRetrievalApplied,
)


class AgentLearningObservationRecorded(HolyfieldsAgentLearningObservationRecorded, BaseEvent):
    """Bloodbank-compatible wrapper for the Holyfields observation model."""


class AgentLearningEpisodeCreated(HolyfieldsAgentLearningEpisodeCreated, BaseEvent):
    """Bloodbank-compatible wrapper for the Holyfields episode model."""


class AgentLearningCandidateExtracted(HolyfieldsAgentLearningCandidateExtracted, BaseEvent):
    """Bloodbank-compatible wrapper for the Holyfields candidate model."""


class AgentLearningCandidateValidated(HolyfieldsAgentLearningCandidateValidated, BaseEvent):
    """Bloodbank-compatible wrapper for the Holyfields validation model."""


class AgentLearningLessonPromoted(HolyfieldsAgentLearningLessonPromoted, BaseEvent):
    """Bloodbank-compatible wrapper for the Holyfields promoted-lesson model."""


class AgentLearningLessonRejected(HolyfieldsAgentLearningLessonRejected, BaseEvent):
    """Bloodbank-compatible wrapper for the Holyfields rejected-lesson model."""


class AgentLearningLessonRolledBack(HolyfieldsAgentLearningLessonRolledBack, BaseEvent):
    """Bloodbank-compatible wrapper for the Holyfields rollback model."""


class AgentLearningRetrievalApplied(HolyfieldsAgentLearningRetrievalApplied, BaseEvent):
    """Bloodbank-compatible wrapper for the Holyfields retrieval model."""


AgentLearningEventType = Literal[
    "agent.learning.observation.recorded",
    "agent.learning.episode.created",
    "agent.learning.candidate.extracted",
    "agent.learning.candidate.validated",
    "agent.learning.lesson.promoted",
    "agent.learning.lesson.rejected",
    "agent.learning.lesson.rolled_back",
    "agent.learning.retrieval.applied",
]


ROUTING_KEYS = {
    "AgentLearningObservationRecorded": "agent.learning.observation.recorded",
    "AgentLearningEpisodeCreated": "agent.learning.episode.created",
    "AgentLearningCandidateExtracted": "agent.learning.candidate.extracted",
    "AgentLearningCandidateValidated": "agent.learning.candidate.validated",
    "AgentLearningLessonPromoted": "agent.learning.lesson.promoted",
    "AgentLearningLessonRejected": "agent.learning.lesson.rejected",
    "AgentLearningLessonRolledBack": "agent.learning.lesson.rolled_back",
    "AgentLearningRetrievalApplied": "agent.learning.retrieval.applied",
}
