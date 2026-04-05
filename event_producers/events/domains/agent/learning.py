"""
Agent learning event payload definitions.

GENERATED FROM HOLYFIELDS SCHEMAS — Do not edit manually.
To update: modify JSON schemas in holyfields/schemas/agent/learning/, regenerate,
and re-export here.

All events are wrapped in EventEnvelope[T] where T is your payload type.
"""

from typing import Literal

from holyfields.compat import (
    AgentLearningCandidateExtracted,
    AgentLearningCandidateValidated,
    AgentLearningEpisodeCreated,
    AgentLearningLessonPromoted,
    AgentLearningLessonRejected,
    AgentLearningLessonRolledBack,
    AgentLearningObservationRecorded,
    AgentLearningRetrievalApplied,
)


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
