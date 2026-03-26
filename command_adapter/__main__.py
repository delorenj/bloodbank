"""
Entry point: python -m command_adapter

Runs the command adapter as a standalone service.
"""
import asyncio
import logging
import signal
import sys

from event_producers.healthz import start_healthz_server
from .config import AdapterConfig
from .consumer import CommandAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("command-adapter")


async def main() -> None:
    config = AdapterConfig()

    logger.info("Command Adapter (GOD-3 / AGENT-CTRL-1)")
    logger.info(f"  RabbitMQ: {config.rabbitmq_url.split('@')[1] if '@' in config.rabbitmq_url else config.rabbitmq_url}")
    logger.info(f"  Redis:    {config.redis_url}")
    logger.info(f"  Hook URL: {config.openclaw_hook_url}")
    logger.info(f"  Agents:   {config.agents or ['*']}")

    adapter = CommandAdapter(config)

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    await adapter.start()

    # Start /healthz endpoint
    await start_healthz_server(
        lambda: adapter._conn is not None and not adapter._conn.is_closed
    )

    logger.info("Command adapter running. Ctrl+C to stop.")

    await stop.wait()
    await adapter.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
