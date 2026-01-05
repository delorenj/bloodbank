"""
Core event processing infrastructure.

This module provides the foundational abstractions for command/event processing:
- abstraction: Base classes for commands, events, and the command pattern
- consumer: RabbitMQ consumer for event ingestion
- manager: Command orchestration and side-effect handling
"""

from event_producers.events.core.abstraction import (
    EventCollector,
    CommandContext,
    Invokable,
    BaseEvent,
    BaseCommand,
)
from event_producers.events.core.consumer import Consumer
from event_producers.events.core.manager import CommandManager

# Alias for backward compatibility if needed
EventConsumer = Consumer

__all__ = [
    # Abstraction layer
    "EventCollector",
    "CommandContext",
    "Invokable",
    "BaseEvent",
    "BaseCommand",
    # Consumer
    "Consumer",
    "EventConsumer",  # Alias
    # Manager
    "CommandManager",
]
