import asyncio
import logging
import signal
from event_producers.events.core.consumer import Consumer
from event_producers.events.core.manager import CommandManager
from event_producers.events.base import EventEnvelope
from event_producers.events.registry import get_registry
from event_producers.rabbit import Publisher

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("CommandProcessor")

async def main():
    # 1. Setup Infrastructure
    publisher = Publisher(enable_correlation_tracking=True)
    await publisher.start()
    
    manager = CommandManager(publisher)
    consumer = Consumer(service_name="command_processor")

    # 2. Auto-discover Events/Commands
    registry = get_registry()
    registry.auto_discover_domains()
    
    # 3. Determine Routing Keys
    # We want to listen to ALL events that map to an Invokable payload
    # This effectively makes this processor the worker for all Commands in the system
    from event_producers.events.core.abstraction import Invokable
    
    command_keys = []
    for domain_name in registry.list_domains():
        for event_type in registry.list_domain_events(domain_name):
            payload_class = registry.get_payload_type(event_type)
            # Check if class implements execute() - strictly speaking check inheritance from Invokable
            if issubclass(payload_class, Invokable):
                command_keys.append(event_type)
                logger.info(f"Registered command handler for: {event_type}")

    if not command_keys:
        logger.warning("No command events found! Consumer will be idle.")

    # 4. Define Message Handler
    async def process_message(payload: dict):
        try:
            # Parse Envelope
            envelope = EventEnvelope(**payload)
            await manager.handle_envelope(envelope)
        except Exception as e:
            logger.error(f"Failed to handle envelope: {e}")
            raise e

    # 5. Start Consumer
    await consumer.start(callback=process_message, routing_keys=command_keys)

    # 6. Keep Alive
    stop_event = asyncio.Event()
    
    def signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    logger.info(f"Command Processor is running. Listening for {len(command_keys)} command types.")
    await stop_event.wait()

    # 7. Cleanup
    await consumer.close()
    await publisher.close()
    logger.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())