"""Master heartbeat tick publisher.

Publishes `system.heartbeat.tick` to Bloodbank every 60 seconds.
Runs as a long-lived FastStream service inside Docker compose.

Event payload:
    tick        int         Monotonic counter (resets on restart)
    timestamp   str         ISO-8601 UTC
    epoch_ms    int         Unix epoch milliseconds
    quarter     str         Q1-Q4
    day_of_week str         Monday-Sunday
    hour        int         0-23 UTC
    minute      int         0-59

Usage:
    python -m heartbeat_tick.publisher

Environment:
    RABBITMQ_URL        amqp connection string
    BLOODBANK_EXCHANGE  exchange name (default: bloodbank.events.v1)
    TICK_INTERVAL_S     seconds between ticks (default: 60)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
from datetime import datetime, timezone
from uuid import uuid4

import aio_pika

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [heartbeat-tick] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "")
EXCHANGE_NAME = os.environ.get("BLOODBANK_EXCHANGE", "bloodbank.events.v1")
TICK_INTERVAL = int(os.environ.get("TICK_INTERVAL_S", "60"))
HOSTNAME = socket.gethostname()

EVENT_TYPE = "system.heartbeat.tick"
ROUTING_KEY = "system.heartbeat.tick"


def _quarter(month: int) -> str:
    return f"Q{(month - 1) // 3 + 1}"


def _build_tick_envelope(tick: int) -> bytes:
    """Build a Bloodbank envelope for a heartbeat tick."""
    import json

    now = datetime.now(timezone.utc)
    envelope = {
        "event_id": str(uuid4()),
        "event_type": EVENT_TYPE,
        "timestamp": now.isoformat(),
        "version": "1.0.0",
        "source": {
            "host": HOSTNAME,
            "type": "scheduled",
            "app": "heartbeat-tick-publisher",
        },
        "correlation_ids": [],
        "payload": {
            "tick": tick,
            "timestamp": now.isoformat(),
            "epoch_ms": int(now.timestamp() * 1000),
            "quarter": _quarter(now.month),
            "day_of_week": now.strftime("%A"),
            "hour": now.hour,
            "minute": now.minute,
        },
    }
    return json.dumps(envelope).encode()


async def run() -> None:
    if not RABBITMQ_URL:
        logger.error("RABBITMQ_URL not set")
        sys.exit(1)

    logger.info(
        "Starting heartbeat tick publisher: exchange=%s interval=%ds",
        EXCHANGE_NAME,
        TICK_INTERVAL,
    )

    # Connect to RabbitMQ with robust reconnection
    connection = await aio_pika.connect_robust(RABBITMQ_URL, heartbeat=30)
    channel = await connection.channel()
    exchange = await channel.declare_exchange(
        EXCHANGE_NAME,
        aio_pika.ExchangeType.TOPIC,
        durable=True,
    )

    logger.info("Connected to RabbitMQ, exchange %s declared", EXCHANGE_NAME)

    tick = 0
    shutdown = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received")
        shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        while not shutdown.is_set():
            tick += 1
            body = _build_tick_envelope(tick)

            await exchange.publish(
                aio_pika.Message(
                    body=body,
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=ROUTING_KEY,
            )

            logger.info("Tick #%d published", tick)

            # Wait for interval or shutdown, whichever comes first
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=TICK_INTERVAL)
            except asyncio.TimeoutError:
                pass  # Normal — interval elapsed, loop continues
    finally:
        await connection.close()
        logger.info("Publisher shut down after %d ticks", tick)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
