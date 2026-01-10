"""
Bloodbank Event Consumer Infrastructure (FastStream)

This module provides the FastStream application and broker instances
configured for the Bloodbank event bus.

Usage:
    from event_producers.consumer import broker
    from event_producers.events.types import FirefliesEventType

    @broker.subscriber("fireflies_queue", exchange="bloodbank.events.v1", routing_key="fireflies.transcript.ready")
    async def handle_ready(msg: FirefliesTranscriptReadyPayload):
        ...
"""

from faststream import FastStream
from faststream.rabbit import RabbitBroker, RabbitExchange, ExchangeType

from event_producers.config import settings

# Configure the RabbitMQ Broker
broker = RabbitBroker(settings.rabbit_url)

# Define the main exchange
# We use a TOPIC exchange as defined in architecture
exchange = RabbitExchange(
    name=settings.exchange_name,
    type=ExchangeType.TOPIC,
    durable=True,
)

# The FastStream application
app = FastStream(broker)

def get_broker() -> RabbitBroker:
    """Get the configured RabbitMQ broker instance."""
    return broker

def get_app() -> FastStream:
    """Get the FastStream application instance."""
    return app