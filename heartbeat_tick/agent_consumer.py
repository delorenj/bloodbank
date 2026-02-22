"""Per-agent heartbeat consumer.

Subscribes to:
    - agent.{name}.inbox  (routing: agent.{name}.#)
    - system.heartbeat.tick (from master tick publisher)

On each tick:
    1. Load heartbeat.json from the agent's workspace
    2. For each enabled check, calculate if it's overdue
    3. Dispatch overdue checks (system_event → OpenClaw, publish → Bloodbank, command → shell)
    4. Track last-run timestamps in a state file

Usage:
    AGENT_NAME=grolf HEARTBEAT_JSON=/path/to/heartbeat.json python -m heartbeat_tick.agent_consumer

Environment:
    AGENT_NAME          Agent identifier (required)
    RABBITMQ_URL        amqp connection string
    BLOODBANK_EXCHANGE  exchange name (default: bloodbank.events.v1)
    HEARTBEAT_JSON      path to heartbeat.json (default: ~/.openclaw/workspace-{agent}/heartbeat.json)
    HEARTBEAT_STATE     path to state file (default: /tmp/heartbeat-{agent}-state.json)
    OPENCLAW_HOOK_URL   URL for system event injection (default: http://localhost:18789)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import aio_pika
from aio_pika import ExchangeType

from .schema import HeartbeatConfig

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [heartbeat-consumer:%(agent)s] %(levelname)s %(message)s",
    stream=sys.stdout,
    defaults={"agent": os.environ.get("AGENT_NAME", "?")},
)
logger = logging.getLogger(__name__)

AGENT_NAME = os.environ.get("AGENT_NAME", "")
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "")
EXCHANGE_NAME = os.environ.get("BLOODBANK_EXCHANGE", "bloodbank.events.v1")
HEARTBEAT_JSON = os.environ.get(
    "HEARTBEAT_JSON",
    os.path.expanduser(f"~/.openclaw/workspace-{AGENT_NAME}/heartbeat.json"),
)
STATE_PATH = os.environ.get(
    "HEARTBEAT_STATE",
    f"/tmp/heartbeat-{AGENT_NAME}-state.json",
)
HOSTNAME = socket.gethostname()


# ─── State management ────────────────────────────────────────────────────────


def _load_state() -> dict[str, float]:
    """Load {check_id: last_run_epoch} from state file."""
    try:
        return json.loads(Path(STATE_PATH).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, float]) -> None:
    Path(STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(STATE_PATH).write_text(json.dumps(state, indent=2))


# ─── Check dispatch ──────────────────────────────────────────────────────────


def _is_overdue(check_id: str, interval_minutes: int, state: dict[str, float]) -> bool:
    last = state.get(check_id, 0)
    return (time.time() - last) >= (interval_minutes * 60)


async def _dispatch_system_event(agent: str, check: Any, exchange: Any) -> bool:
    """Publish a system event that triggers the agent's session."""
    logger.info("Dispatching system_event: %s → %s", check.id, agent)

    envelope = {
        "event_id": str(uuid4()),
        "event_type": f"agent.{agent}.heartbeat.dispatch",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
        "source": {
            "host": HOSTNAME,
            "type": "heartbeat",
            "app": "heartbeat-agent-consumer",
        },
        "correlation_ids": [],
        "payload": {
            "agent": agent,
            "check_id": check.id,
            "description": check.description,
            "prompt": check.prompt,
            "action": "system_event",
        },
    }

    await exchange.publish(
        aio_pika.Message(
            body=json.dumps(envelope).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=f"agent.{agent}.heartbeat.dispatch",
    )
    return True


async def _dispatch_publish(agent: str, check: Any, exchange: Any) -> bool:
    """Publish a custom event to Bloodbank."""
    logger.info("Dispatching publish: %s → %s", check.id, check.event_type)

    envelope = {
        "event_id": str(uuid4()),
        "event_type": check.event_type or f"agent.{agent}.heartbeat.{check.id}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
        "source": {
            "host": HOSTNAME,
            "type": "heartbeat",
            "app": "heartbeat-agent-consumer",
        },
        "correlation_ids": [],
        "payload": {
            "agent": agent,
            "check_id": check.id,
            "description": check.description,
        },
    }

    routing_key = check.event_type or f"agent.{agent}.heartbeat.{check.id}"
    await exchange.publish(
        aio_pika.Message(
            body=json.dumps(envelope).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=routing_key,
    )
    return True


def _dispatch_command(agent: str, check: Any) -> bool:
    """Run a shell command."""
    logger.info("Dispatching command: %s → %s", check.id, check.command)
    try:
        result = subprocess.run(
            check.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            logger.error(
                "Command failed (rc=%d): %s\nstderr: %s",
                result.returncode,
                check.id,
                result.stderr[:300],
            )
            return False
        return True
    except Exception as e:
        logger.error("Command error: %s — %s", check.id, e)
        return False


# ─── Tick handler ────────────────────────────────────────────────────────────


async def handle_tick(
    tick_payload: dict[str, Any],
    config: HeartbeatConfig,
    exchange: Any,
) -> None:
    """Process a heartbeat tick: check all definitions, dispatch overdue ones."""
    day = tick_payload.get("day_of_week", "")
    hour = tick_payload.get("hour", 0)
    quarter = tick_payload.get("quarter", "")
    tick_num = tick_payload.get("tick", 0)

    state = _load_state()
    dispatched = 0

    for check in config.checks:
        if not check.enabled:
            continue

        # Check conditions (time of day, day of week, quarter)
        if not check.conditions.matches(day, hour, quarter):
            continue

        # Check if overdue
        if not _is_overdue(check.id, check.interval_minutes, state):
            continue

        # Dispatch
        success = False
        if check.action == "system_event":
            success = await _dispatch_system_event(config.agent, check, exchange)
        elif check.action == "publish":
            success = await _dispatch_publish(config.agent, check, exchange)
        elif check.action == "command":
            success = _dispatch_command(config.agent, check)
        else:
            logger.warning("Unknown action: %s for check %s", check.action, check.id)

        if success:
            state[check.id] = time.time()
            dispatched += 1

    _save_state(state)

    if dispatched > 0:
        logger.info("Tick #%d: dispatched %d checks", tick_num, dispatched)
    else:
        logger.debug("Tick #%d: nothing overdue", tick_num)


# ─── Main consumer loop ─────────────────────────────────────────────────────


async def run() -> None:
    if not AGENT_NAME:
        logger.error("AGENT_NAME not set")
        sys.exit(1)
    if not RABBITMQ_URL:
        logger.error("RABBITMQ_URL not set")
        sys.exit(1)

    # Load heartbeat config
    config_path = Path(HEARTBEAT_JSON)
    if not config_path.exists():
        logger.error("heartbeat.json not found: %s", config_path)
        sys.exit(1)

    config = HeartbeatConfig.from_json(str(config_path))
    logger.info(
        "Loaded heartbeat.json: agent=%s, %d checks",
        config.agent,
        len(config.checks),
    )

    # Connect to RabbitMQ
    connection = await aio_pika.connect_robust(RABBITMQ_URL, heartbeat=30)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=1)

    exchange = await channel.declare_exchange(
        EXCHANGE_NAME,
        ExchangeType.TOPIC,
        durable=True,
    )

    # Declare agent inbox queue
    inbox_name = f"agent.{AGENT_NAME}.inbox"
    inbox = await channel.declare_queue(inbox_name, durable=True)

    # Bind to agent-specific routing AND system tick
    await inbox.bind(exchange, routing_key=f"agent.{AGENT_NAME}.#")
    await inbox.bind(exchange, routing_key="system.heartbeat.tick")

    logger.info(
        "Consumer ready: queue=%s, bindings=[agent.%s.#, system.heartbeat.tick]",
        inbox_name,
        AGENT_NAME,
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
                # Reload config on every tick (hot-reload support)
                try:
                    fresh_config = HeartbeatConfig.from_json(str(config_path))
                except Exception:
                    fresh_config = config

                await handle_tick(payload, fresh_config, exchange)
            else:
                # Non-tick message — log it for now
                logger.info(
                    "Received event: %s (routing: %s)",
                    event_type,
                    message.routing_key,
                )

            await message.ack()
        except Exception as e:
            logger.error("Error processing message: %s", e)
            try:
                await message.reject(requeue=False)
            except Exception:
                pass

    await inbox.consume(_process)
    logger.info("Consuming... waiting for ticks")

    # Wait for shutdown signal
    await shutdown.wait()
    await connection.close()
    logger.info("Consumer shut down")


def main() -> None:
    import signal
    asyncio.run(run())


if __name__ == "__main__":
    main()
