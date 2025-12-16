"""
Base classes for all Bloodbank events.
"""

from pydantic import BaseModel
from datetime import datetime
from uuid import UUID


class BaseEvent(BaseModel):
    """
    Base class for all events in the Bloodbank system.

    All events must derive from this class to ensure consistent structure.
    """

    class Config:
        json_encoders = {UUID: str, datetime: lambda v: v.isoformat()}

    @classmethod
    def get_routing_key(cls) -> str:
        """
        Get the routing key for this event type.

        Override this in subclasses to define the event's routing key.
        Format: <domain>.<entity>.<past-tense-action>
        Example: "fireflies.transcript.ready"
        """
        raise NotImplementedError(
            f"{cls.__name__} must implement get_routing_key() class method"
        )

    @classmethod
    def get_domain(cls) -> str:
        """
        Get the domain this event belongs to.

        Extracted from the routing key.
        """
        routing_key = cls.get_routing_key()
        return routing_key.split('.')[0]

    @classmethod
    def is_command(cls) -> bool:
        """
        Check if this event is a command (mutable operation).

        Returns True if this event derives from Command class.
        """
        return issubclass(cls, Command)


class Command(BaseEvent):
    """
    Base class for command events (events that trigger mutations).

    Commands follow naming convention: <domain>.<entity>.<action>
    Example: "github.pr.merge"

    Commands have their own exchange and are bound to worker queues.
    """

    @classmethod
    def get_routing_key(cls) -> str:
        """
        Get the routing key for this command.

        Commands use imperative verbs (create, update, delete)
        instead of past tense (created, updated, deleted).
        """
        raise NotImplementedError(
            f"{cls.__name__} must implement get_routing_key() class method"
        )
