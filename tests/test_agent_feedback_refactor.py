"""
Test suite for ADR-0002: Agent Feedback Event Architecture

Validates that AgentFeedbackRequested is properly refactored from BaseCommand
to BaseEvent and that the command processor doesn't auto-register it.
"""

import pytest
from event_producers.events.registry import get_registry
from event_producers.events.core.abstraction import Invokable, BaseEvent, BaseCommand
from event_producers.events.domains.agent.feedback import (
    AgentFeedbackRequested,
    AgentFeedbackResponse,
)


class TestAgentFeedbackRefactoring:
    """Test suite validating the agent feedback architectural refactoring."""

    def test_agent_feedback_requested_is_base_event(self):
        """Verify AgentFeedbackRequested extends BaseEvent, not BaseCommand."""
        assert issubclass(AgentFeedbackRequested, BaseEvent)
        assert not issubclass(AgentFeedbackRequested, BaseCommand)

    def test_agent_feedback_requested_is_not_invokable(self):
        """Verify AgentFeedbackRequested is NOT Invokable (no execute method)."""
        assert not issubclass(AgentFeedbackRequested, Invokable)

    def test_agent_feedback_requested_no_execute_method(self):
        """Verify AgentFeedbackRequested does not have an execute() method."""
        assert not hasattr(AgentFeedbackRequested, "execute")

    def test_agent_feedback_response_is_base_event(self):
        """Verify AgentFeedbackResponse is a BaseEvent."""
        assert issubclass(AgentFeedbackResponse, BaseEvent)
        assert not issubclass(AgentFeedbackResponse, BaseCommand)

    def test_registry_contains_agent_feedback_events(self):
        """Verify both agent feedback events are registered in the event registry."""
        registry = get_registry()
        registry.auto_discover_domains()

        # Both events should be registered
        requested_class = registry.get_payload_type("agent.feedback.requested")
        response_class = registry.get_payload_type("agent.feedback.response")

        assert requested_class == AgentFeedbackRequested
        assert response_class == AgentFeedbackResponse

    def test_command_processor_does_not_discover_agent_feedback_requested(self):
        """
        Verify command processor auto-discovery excludes agent.feedback.requested.

        This simulates the command processor's discovery logic to ensure
        AgentFeedbackRequested is NOT picked up as a command.
        """
        registry = get_registry()
        registry.auto_discover_domains()

        # Simulate command processor discovery
        command_events = []
        for domain_name in registry.list_domains():
            for event_type in registry.list_domain_events(domain_name):
                payload_class = registry.get_payload_type(event_type)
                if issubclass(payload_class, Invokable):
                    command_events.append(event_type)

        # agent.feedback.requested should NOT be in the command list
        assert "agent.feedback.requested" not in command_events

        # agent.thread.prompt should still be a command
        assert "agent.thread.prompt" in command_events

    def test_agent_feedback_requested_can_be_instantiated(self):
        """Verify AgentFeedbackRequested can be instantiated with valid data."""
        request = AgentFeedbackRequested(
            agent_id="test-agent-123",
            message="Test feedback request",
            letta_agent_id="letta-456",
            context={"session_id": "abc"},
            tags=["test", "validation"],
        )

        assert request.agent_id == "test-agent-123"
        assert request.message == "Test feedback request"
        assert request.letta_agent_id == "letta-456"
        assert request.context == {"session_id": "abc"}
        assert request.tags == ["test", "validation"]

    def test_agent_feedback_response_can_be_instantiated(self):
        """Verify AgentFeedbackResponse can be instantiated with valid data."""
        response = AgentFeedbackResponse(
            agent_id="test-agent-123",
            letta_agent_id="letta-456",
            response="This is the agent's feedback",
            status="ok",
            metadata={"tokens_used": 150},
        )

        assert response.agent_id == "test-agent-123"
        assert response.letta_agent_id == "letta-456"
        assert response.response == "This is the agent's feedback"
        assert response.status == "ok"
        assert response.metadata == {"tokens_used": 150}

    def test_adr_0002_compliance(self):
        """
        Comprehensive ADR-0002 compliance test.

        Validates all architectural decisions from ADR-0002:
        1. AgentFeedbackRequested is a pure event (BaseEvent)
        2. It is NOT invokable (no execute method)
        3. Command processor will not auto-register it
        4. Event registry still contains it
        5. Both events can be instantiated
        """
        # 1. Pure event, not command
        assert issubclass(AgentFeedbackRequested, BaseEvent)
        assert not issubclass(AgentFeedbackRequested, BaseCommand)

        # 2. Not invokable
        assert not issubclass(AgentFeedbackRequested, Invokable)

        # 3. Command processor exclusion
        registry = get_registry()
        registry.auto_discover_domains()

        command_events = [
            event_type
            for domain in registry.list_domains()
            for event_type in registry.list_domain_events(domain)
            if issubclass(registry.get_payload_type(event_type), Invokable)
        ]

        assert "agent.feedback.requested" not in command_events

        # 4. Still in event registry
        assert registry.get_payload_type("agent.feedback.requested") == AgentFeedbackRequested
        assert registry.get_payload_type("agent.feedback.response") == AgentFeedbackResponse

        # 5. Can be instantiated
        request = AgentFeedbackRequested(
            agent_id="test", message="test message"
        )
        response = AgentFeedbackResponse(agent_id="test", response="test response")

        assert request.agent_id == "test"
        assert response.agent_id == "test"
