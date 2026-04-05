"""Test suite for the agent learning event registry integration."""

from event_producers.events.registry import get_registry
from event_producers.events.types import EVENT_TYPE_TO_PAYLOAD
from event_producers.events.domains.agent.learning import (
    AgentLearningCandidateExtracted,
    AgentLearningCandidateValidated,
    AgentLearningEpisodeCreated,
    AgentLearningLessonPromoted,
    AgentLearningLessonRejected,
    AgentLearningLessonRolledBack,
    AgentLearningObservationRecorded,
    AgentLearningRetrievalApplied,
)


EXPECTED_EVENT_TYPES = {
    "agent.learning.observation.recorded": AgentLearningObservationRecorded,
    "agent.learning.episode.created": AgentLearningEpisodeCreated,
    "agent.learning.candidate.extracted": AgentLearningCandidateExtracted,
    "agent.learning.candidate.validated": AgentLearningCandidateValidated,
    "agent.learning.lesson.promoted": AgentLearningLessonPromoted,
    "agent.learning.lesson.rejected": AgentLearningLessonRejected,
    "agent.learning.lesson.rolled_back": AgentLearningLessonRolledBack,
    "agent.learning.retrieval.applied": AgentLearningRetrievalApplied,
}


def test_registry_contains_agent_learning_domain():
    registry = get_registry()
    registry.auto_discover_domains()

    assert "agent/learning" in registry.list_domains()
    assert set(registry.list_domain_events("agent/learning")) == set(EXPECTED_EVENT_TYPES)


def test_registry_payload_types_match_learning_models():
    registry = get_registry()
    registry.auto_discover_domains()

    for event_type, payload_class in EXPECTED_EVENT_TYPES.items():
        assert registry.get_payload_type(event_type) == payload_class


def test_event_type_mapping_contains_learning_models():
    for event_type, payload_class in EXPECTED_EVENT_TYPES.items():
        assert EVENT_TYPE_TO_PAYLOAD[event_type] == payload_class
