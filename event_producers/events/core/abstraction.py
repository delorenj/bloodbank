from abc import ABC, abstractmethod
from typing import Any, List, Generic, TypeVar, Optional, Union
from pydantic import BaseModel
from datetime import datetime
from uuid import UUID

from event_producers.events.base import EventEnvelope, AgentContext

# Generic type for the command result
R = TypeVar("R")


class EventCollector:
    """
    Collects side-effect events produced during command execution.
    Follows the FIFO principle for side effect management.
    """

    def __init__(self):
        self._events: List[EventEnvelope] = []

    def add(self, event: EventEnvelope) -> None:
        """Add a side-effect event to the queue."""
        self._events.append(event)

    def collect(self) -> List[EventEnvelope]:
        """Retrieve and clear the collected events."""
        events = list(self._events)
        self._events.clear()
        return events

    @property
    def count(self) -> int:
        return len(self._events)


class CommandContext(BaseModel):
    """
    Contextual information required for command execution.
    Passed to execute() method of commands.
    """
    correlation_id: UUID
    source_app: str
    agent_context: Optional[AgentContext] = None
    timestamp: datetime


class Invokable(ABC, Generic[R]):
    """
    Interface defining the contract for all command implementations.
    """

    @abstractmethod
    def execute(self, context: CommandContext, collector: EventCollector) -> R:
        """
        Execute the command logic.
        
        Args:
            context: Execution context (correlation ID, source, etc.)
            collector: EventCollector to register side effects
            
        Returns:
            A result of type R
        """
        pass

    def rollback(self, context: CommandContext) -> None:
        """
        Rollback any changes made by the command in case of failure.
        Default implementation is no-op.
        """
        pass


class BaseEvent(BaseModel):
    """
    Base class for all domain events and commands (payloads).
    Ensures they are Pydantic models.
    """
    pass


class BaseCommand(BaseEvent, Invokable[R]):
    """
    Base class for Commands.
    A Command is a Pydantic model (Data) that is also Invokable (Behavior).
    """
    pass