import asyncio
from urllib.parse import urlparse, urlunparse

import orjson
import aio_pika

from .config import settings


def _redacted_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        return url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    new_netloc = host + port
    redacted = parsed._replace(netloc=new_netloc)
    return urlunparse(redacted)


class Publisher:
    def __init__(self):
        self._conn = None
        self._channel = None
        self._exchange = None
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self):
        if self._started:
            return
        async with self._lock:
            if self._started:
                return
            if not settings.rabbit_url:
                raise RuntimeError("RABBIT_URL is not configured; set the environment variable.")
            try:
                self._conn = await asyncio.wait_for(
                    aio_pika.connect_robust(settings.rabbit_url), timeout=10
                )
                self._channel = await self._conn.channel(publisher_confirms=True)
                self._exchange = await self._channel.declare_exchange(
                    settings.exchange_name, aio_pika.ExchangeType.TOPIC, durable=True
                )
            except Exception as exc:  # pragma: no cover - surfaced in startup
                safe_url = _redacted_url(settings.rabbit_url)
                raise RuntimeError(
                    f"Failed to connect to RabbitMQ at '{safe_url}': {exc}"
                ) from exc
            self._started = True

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
        async with self._lock:
            if self._channel and not self._channel.is_closed:
                await self._channel.close()
            if self._conn:
                await self._conn.close()
            self._conn = None
            self._channel = None
            self._exchange = None
            self._started = False
