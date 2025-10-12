import asyncio
import json
import os
import signal
from contextlib import asynccontextmanager, suppress

import aio_pika
from aio_pika.abc import AbstractQueue

from config import settings


async def _connect():
    connection = await aio_pika.connect_robust(settings.rabbit_url)
    channel = await connection.channel()
    exchange = await channel.declare_exchange(
        settings.exchange_name,
        aio_pika.ExchangeType.TOPIC,
        durable=True,
    )
    queue = await channel.declare_queue(
        os.getenv("ARTIFACT_QUEUE", "artifact-logger"), durable=True
    )
    await queue.bind(
        exchange,
        routing_key=os.getenv("ARTIFACT_ROUTING_KEY", "artifact.#"),
    )
    return connection, channel, queue


@asynccontextmanager
async def _consumer():
    connection, channel, queue = await _connect()
    try:
        yield queue
    finally:
        await channel.close()
        await connection.close()


async def _handle_messages(queue: AbstractQueue) -> None:
    async with queue.iterator() as queue_iter:
        async for message in queue_iter:
            with message.process():
                raw = message.body.decode()
                try:
                    pretty = json.dumps(json.loads(raw), indent=2)
                except json.JSONDecodeError:
                    pretty = raw
                print(f"[{message.routing_key}] {pretty}")


async def _run() -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _stop() -> None:
        if not stop_event.is_set():
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    async with _consumer() as queue:
        handler = asyncio.create_task(_handle_messages(queue))
        await stop_event.wait()
        handler.cancel()
        with suppress(asyncio.CancelledError):
            await handler


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
