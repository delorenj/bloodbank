"""Multi-agent heartbeat consumer.

Runs tick-driven checks for MULTIPLE agents in a single process.
Each agent's heartbeat.json is loaded and checked independently on every tick.

Much more efficient than running 11 separate consumer containers.

Usage:
    AGENT_CONFIGS=/configs python -m heartbeat_tick.multi_consumer

Environment:
    AGENT_CONFIGS       Dir containing {agent}.heartbeat.json files
    RABBITMQ_URL        amqp connection string
    BLOODBANK_EXCHANGE  exchange name (default: bloodbank.events.v1)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import aio_pika
from aio_pika import ExchangeType

from .schema import HeartbeatConfig

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [heartbeat-consumer] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "")
EXCHANGE_NAME = os.environ.get("BLOODBANK_EXCHANGE", "bloodbank.events.v1")
AGENT_CONFIGS = os.environ.get("AGENT_CONFIGS", "/configs")
BLOODBANK_API = os.environ.get("BLOODBANK_API", "http://bloodbank:8682")
HOSTNAME = socket.gethostname()

# State per agent: {agent_name: {check_id: last_run_epoch}}
_state: dict[str, dict[str, float]] = {}
STATE_PATH = Path(os.environ.get("HEARTBEAT_STATE", "/tmp/heartbeat-multi-state.json"))


def _load_state() -> None:
    global _state
    try:
        _state = json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        _state = {}


def _save_state() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(_state, indent=2))


def _is_overdue(agent: str, check_id: str, interval_minutes: int) -> bool:
    last = _state.get(agent, {}).get(check_id, 0)
    return (time.time() - last) >= (interval_minutes * 60)


def _mark_fired(agent: str, check_id: str) -> None:
    _state.setdefault(agent, {})[check_id] = time.time()


def _load_configs() -> list[HeartbeatConfig]:
    """Load all heartbeat.json files from AGENT_CONFIGS dir."""
    configs = []
    config_dir = Path(AGENT_CONFIGS)
    if not config_dir.exists():
        logger.error("Config dir not found: %s", config_dir)
        return configs

    for f in sorted(config_dir.glob("*.json")):
        try:
            cfg = HeartbeatConfig.from_json(str(f))
            configs.append(cfg)
            logger.info("Loaded config: %s (%d checks)", cfg.agent, len(cfg.checks))
        except Exception as e:
            logger.error("Failed to load %s: %s", f, e)

    return configs


def _publish_event(event_type: str, payload: dict) -> bool:
    """Publish event to Bloodbank API."""
    body = json.dumps({
        "event_type": event_type,
        "event_id": str(uuid4()),
        "payload": payload,
        "source": {"host": HOSTNAME, "type": "heartbeat", "app": "heartbeat-multi-consumer"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
    }).encode()
    try:
        req = urllib.request.Request(
            f"{BLOODBANK_API}/events/custom",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception as e:
        logger.warning("Failed to publish %s: %s", event_type, e)
        return False


async def _handle_tick(tick_payload: dict[str, Any], configs: list[HeartbeatConfig]) -> None:
    """Process tick for all agents."""
    day = tick_payload.get("day_of_week", "")
    hour = tick_payload.get("hour", 0)
    quarter = tick_payload.get("quarter", "")
    tick_num = tick_payload.get("tick", 0)

    total_dispatched = 0

    for cfg in configs:
        for check in cfg.checks:
            if not check.enabled:
                continue
            if not check.conditions.matches(day, hour, quarter):
                continue
            if not _is_overdue(cfg.agent, check.id, check.interval_minutes):
                continue

            # Dispatch based on action type
            success = False
            if check.action == "system_event":
                # Publish dispatch event — OpenClaw cron or session injection picks it up
                success = _publish_event(
                    f"agent.{cfg.agent}.heartbeat.dispatch",
                    {
                        "agent": cfg.agent,
                        "check_id": check.id,
                        "description": check.description,
                        "prompt": check.prompt,
                        "action": "system_event",
                        "dispatched_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                if success:
                    logger.info("Dispatched: %s/%s (system_event)", cfg.agent, check.id)
            elif check.action == "publish":
                success = _publish_event(
                    check.event_type or f"agent.{cfg.agent}.heartbeat.{check.id}",
                    {
                        "agent": cfg.agent,
                        "check_id": check.id,
                        "description": check.description,
                    },
                )
            elif check.action == "command":
                import subprocess
                try:
                    result = subprocess.run(
                        check.command, shell=True,
                        capture_output=True, text=True, timeout=300,
                    )
                    success = result.returncode == 0
                except Exception as e:
                    logger.error("Command failed for %s/%s: %s", cfg.agent, check.id, e)

            if success:
                _mark_fired(cfg.agent, check.id)
                total_dispatched += 1

    _save_state()

    if total_dispatched > 0:
        logger.info("Tick #%d: dispatched %d checks across %d agents", tick_num, total_dispatched, len(configs))


async def run() -> None:
    if not RABBITMQ_URL:
        logger.error("RABBITMQ_URL not set")
        sys.exit(1)

    # Load all agent configs
    configs = _load_configs()
    if not configs:
        logger.error("No heartbeat configs found in %s", AGENT_CONFIGS)
        sys.exit(1)

    _load_state()

    # Connect to RabbitMQ
    connection = await aio_pika.connect_robust(RABBITMQ_URL, heartbeat=30)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=1)

    exchange = await channel.declare_exchange(
        EXCHANGE_NAME, ExchangeType.TOPIC, durable=True,
    )

    # Single queue for tick events
    tick_queue = await channel.declare_queue(
        "heartbeat.multi-consumer.ticks",
        durable=True,
    )
    await tick_queue.bind(exchange, routing_key="system.heartbeat.tick")

    logger.info(
        "Multi-consumer ready: %d agents, listening for ticks",
        len(configs),
    )

    shutdown = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received")
        shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    async def _process(message: aio_pika.IncomingMessage) -> None:
        try:
            body = json.loads(message.body.decode())
            event_type = body.get("event_type", "")
            payload = body.get("payload", {})

            if event_type == "system.heartbeat.tick":
                # Reload configs every tick (hot-reload)
                fresh = _load_configs() or configs
                await _handle_tick(payload, fresh)

            await message.ack()
        except Exception as e:
            logger.error("Error processing tick: %s", e)
            try:
                await message.reject(requeue=False)
            except Exception:
                pass

    await tick_queue.consume(_process)
    logger.info("Consuming ticks... Ctrl+C to stop")
    await shutdown.wait()
    await connection.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
