import asyncio
import logging
import orjson
import aio_pika
from typing import Callable, Awaitable, List
from config import settings

logger = logging.getLogger(__name__)

class Consumer:
    """
    Generic RabbitMQ Consumer.
    """

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.queue_name = f"{settings.exchange_name}.{service_name}"
        self._conn = None
        self._channel = None
        self._queue = None
        self._callback = None

    async def start(self, callback: Callable[[dict], Awaitable[None]], routing_keys: List[str]):
        """
        Start the consumer.
        
        Args:
            callback: Async function to process the message payload (dict).
            routing_keys: List of routing keys to bind the queue to.
        """
        self._callback = callback
        
        try:
            self._conn = await asyncio.wait_for(
                aio_pika.connect_robust(settings.rabbit_url), timeout=10
            )
            self._channel = await self._conn.channel()
            
            # Set generic QoS
            await self._channel.set_qos(prefetch_count=10)

            exchange = await self._channel.declare_exchange(
                settings.exchange_name, aio_pika.ExchangeType.TOPIC, durable=True
            )

            self._queue = await self._channel.declare_queue(
                self.queue_name, durable=True
            )

            for key in routing_keys:
                await self._queue.bind(exchange, routing_key=key)
                logger.info(f"Bound queue {self.queue_name} to key: {key}")

            # Start consuming
            await self._queue.consume(self._on_message)
            logger.info(f"Consumer started for service: {self.service_name}")

        except Exception as e:
            logger.error(f"Failed to start consumer: {e}")
            raise e

    async def _on_message(self, message: aio_pika.IncomingMessage):
        async with message.process():
            try:
                payload = orjson.loads(message.body)
                await self._callback(payload)
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                # Ideally, we might want to dead-letter this if it fails repeatedly
                # For now, message.process() handles ack/nack based on exception?
                # Actually, 'async with message.process()' auto-acks on exit if no exception,
                # and nacks if exception is raised. 
                raise e

    async def close(self):
        if self._conn:
            await self._conn.close()
