import asyncio
import json
import orjson
import aio_pika
from .config import settings


class Publisher:
    def __init__(self):
        self._conn = None
        self._channel = None
        self._exchange = None

    async def start(self):
        self._conn = await aio_pika.connect_robust(settings.rabbit_url)
        self._channel = await self._conn.channel(publisher_confirms=True)
        self._exchange = await self._channel.declare_exchange(
            settings.exchange_name, aio_pika.ExchangeType.TOPIC, durable=True
        )

    async def publish(
        self,
        routing_key: str,
        body: dict,
        message_id: str | None = None,
        correlation_id: str | None = None,
    ):
        if not self._exchange:
            await self.start()
        payload = orjson.dumps(body)
        msg = aio_pika.Message(
            payload,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=message_id or body.get("id"),
            correlation_id=correlation_id or body.get("correlation_id"),
            content_type="application/json",
            content_encoding="utf-8",
        )
        await self._exchange.publish(msg, routing_key=routing_key)

    async def close(self):
        if self._conn:
            await self._conn.close()
