import logging
import asyncio
from typing import Any
from rabbit import Publisher
from event_producers.events.base import EventEnvelope
from event_producers.events.core.abstraction import Invokable, CommandContext, EventCollector
from event_producers.events.registry import get_registry

logger = logging.getLogger(__name__)


class CommandManager:
    """
    The Central Orchestrator (TransactionManager equivalent).
    Coordinatest the lifecycle: Event -> Command -> Execution -> Side Effects.
    """

    def __init__(self, publisher: Publisher):
        self.publisher = publisher
        self.registry = get_registry()

    async def handle_envelope(self, envelope: EventEnvelope) -> None:
        """
        Entry point for processing an incoming event envelope.
        """
        try:
            # 1. Re-hydrate the Payload Object using the Registry
            event_type = envelope.event_type
            payload_class = self.registry.get_payload_type(event_type)

            if not payload_class:
                logger.warning(f"No payload class registered for event type: {event_type}")
                return

            # Construct the payload object from the dictionary
            # envelope.payload is currently a generic type (often dict when deserialized)
            if isinstance(envelope.payload, dict):
                payload_obj = payload_class(**envelope.payload)
            else:
                payload_obj = envelope.payload

            # 2. Check if it is an Invokable Command
            if isinstance(payload_obj, Invokable):
                logger.info(f"Executing command for event: {event_type}")
                await self._execute_command(payload_obj, envelope)
            else:
                logger.debug(f"Event {event_type} is not executable (Fact). Ignoring.")

        except Exception as e:
            logger.error(f"Error processing envelope {envelope.event_id}: {e}", exc_info=True)
            # TODO: Emit Error Event?

    async def _execute_command(self, command: Invokable, envelope: EventEnvelope) -> None:
        """
        Executes the command and publishes side effects.
        """
        # Create Context
        context = CommandContext(
            correlation_id=envelope.event_id,
            source_app=envelope.source.app or "unknown",
            agent_context=envelope.agent_context,
            timestamp=envelope.timestamp
        )
        
        # Create Collector
        collector = EventCollector()

        try:
            # Execute logic
            # Handle both sync and async execute methods
            if asyncio.iscoroutinefunction(command.execute):
                result = await command.execute(context, collector)
            else:
                result = command.execute(context, collector)

            # 3. Collect and Publish Side Effects
            side_effects = collector.collect()
            if side_effects:
                logger.info(f"Publishing {len(side_effects)} side effects for {command.__class__.__name__}")
                for event in side_effects:
                    await self.publisher.publish(
                        routing_key=event.event_type,
                        body=event.model_dump(), # Ensure we dump to dict/json
                        event_id=event.event_id,
                        parent_event_ids=[context.correlation_id]
                    )

        except Exception as e:
            logger.error(f"Command execution failed: {e}")
            command.rollback(context)
            raise e