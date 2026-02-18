#!/usr/bin/env python3
"""
Bloodbank Heartbeat System.

Cron-driven event publisher that reads a schedule config, checks the current
time slot, and fires matching Bloodbank events or runs commands directly.

Idempotency: Each firing is keyed as YYYYmmdd_HHMM. If that key has already
been fired today, it's skipped. Date rolls over → clean slate.

Usage:
    * * * * * cd ~/code/33GOD/bloodbank && uv run python -m heartbeat.heartbeat

Environment:
    RABBITMQ_URL        amqp connection string (required)
    BLOODBANK_EXCHANGE  exchange name (default: bloodbank.events.v1)
    HEARTBEAT_SCHEDULE  path to schedule JSON (default: heartbeat/heartbeat-schedule.json)
    HEARTBEAT_STATE     path to state file (default: heartbeat/.heartbeat-state.json)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import aio_pika
import orjson

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [heartbeat] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────────────

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "")
EXCHANGE_NAME = os.environ.get("BLOODBANK_EXCHANGE", "bloodbank.events.v1")

SCRIPT_DIR = Path(__file__).parent
SCHEDULE_PATH = Path(
    os.environ.get("HEARTBEAT_SCHEDULE", SCRIPT_DIR / "heartbeat-schedule.json")
)
STATE_PATH = Path(
    os.environ.get("HEARTBEAT_STATE", SCRIPT_DIR / ".heartbeat-state.json")
)

# Agent name → routing key mapping
AGENT_ROUTING = {
    "global": "system.heartbeat",
    "cack": "agent.cack.heartbeat",
    "grolf": "agent.grolf.heartbeat",
    "lenoon": "agent.lenoon.heartbeat",
    "rar": "agent.rar.heartbeat",
    "rererere": "agent.rererere.heartbeat",
    "tonny": "agent.tonny.heartbeat",
}


# ─── State management ────────────────────────────────────────────────────────


def _load_state() -> dict:
    """Load fired-keys state. Returns {date_str: [keys]}."""
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _already_fired(state: dict, idempotency_key: str, today: str) -> bool:
    return idempotency_key in state.get(today, [])


def _mark_fired(state: dict, idempotency_key: str, today: str) -> None:
    state.setdefault(today, []).append(idempotency_key)
    # Prune old dates (keep only today)
    for k in list(state.keys()):
        if k != today:
            del state[k]


# ─── Schedule loading ─────────────────────────────────────────────────────────


def load_schedule() -> dict:
    if not SCHEDULE_PATH.exists():
        logger.error("Schedule file not found: %s", SCHEDULE_PATH)
        sys.exit(1)
    return json.loads(SCHEDULE_PATH.read_text())


# ─── Event publishing ─────────────────────────────────────────────────────────


def _build_envelope(
    event_type: str,
    routing_key: str,
    payload: dict,
) -> bytes:
    envelope = {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
        "source": {
            "host": socket.gethostname(),
            "type": "scheduled",
            "app": "heartbeat",
        },
        "correlation_ids": [],
        "payload": payload,
    }
    return orjson.dumps(envelope)


async def publish_prompt(
    channel: aio_pika.abc.AbstractChannel,
    exchange: aio_pika.abc.AbstractExchange,
    entry: dict,
    hhmm: str,
    idempotency_key: str,
) -> None:
    sink = entry["sink"]
    routing_key = AGENT_ROUTING.get(sink, f"agent.{sink}.heartbeat")
    event_type = routing_key

    payload = {
        "sink": sink,
        "description": entry.get("description", ""),
        "prompt": entry["prompt"],
        "schedule_key": hhmm,
        "idempotency_key": idempotency_key,
    }

    body = _build_envelope(event_type, routing_key, payload)

    await exchange.publish(
        aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=routing_key,
    )
    logger.info("Published prompt → %s (%s)", routing_key, entry.get("description", ""))


def run_command(entry: dict, hhmm: str, idempotency_key: str) -> None:
    cmd = [entry["command"]] + entry.get("args", [])
    description = entry.get("description", " ".join(cmd))
    logger.info("Running command: %s (%s)", " ".join(cmd), description)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            logger.info("Command succeeded: %s", description)
        else:
            logger.error(
                "Command failed (rc=%d): %s\nstderr: %s",
                result.returncode,
                description,
                result.stderr[:500] if result.stderr else "(none)",
            )
    except subprocess.TimeoutExpired:
        logger.error("Command timed out (300s): %s", description)
    except FileNotFoundError:
        logger.error("Command not found: %s", cmd[0])


# ─── Main ─────────────────────────────────────────────────────────────────────


async def main() -> None:
    now = datetime.now()
    hhmm = now.strftime("%H%M")
    today = now.strftime("%Y%m%d")

    logger.info("Heartbeat tick: %s_%s", today, hhmm)

    schedule = load_schedule()
    entries = schedule.get(hhmm, [])

    if not entries:
        logger.debug("No entries for %s", hhmm)
        return

    state = _load_state()
    has_prompts = any("prompt" in e for e in entries)

    # Connect to RabbitMQ only if we have prompt entries to publish
    connection = None
    channel = None
    exchange = None

    if has_prompts:
        if not RABBITMQ_URL:
            logger.error("RABBITMQ_URL not set — cannot publish prompt events")
            sys.exit(1)

        connection = await aio_pika.connect_robust(RABBITMQ_URL)
        channel = await connection.channel()
        exchange = await channel.declare_exchange(
            EXCHANGE_NAME,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )

    try:
        for i, entry in enumerate(entries):
            idempotency_key = f"{today}_{hhmm}_{i}"

            if _already_fired(state, idempotency_key, today):
                logger.debug("Skipping (already fired): %s", idempotency_key)
                continue

            if "prompt" in entry:
                await publish_prompt(channel, exchange, entry, hhmm, idempotency_key)
            elif "command" in entry:
                run_command(entry, hhmm, idempotency_key)
            else:
                logger.warning("Entry has neither prompt nor command: %s", entry)
                continue

            _mark_fired(state, idempotency_key, today)

        _save_state(state)
    finally:
        if connection:
            await connection.close()

    logger.info("Heartbeat complete: %d entries processed for %s", len(entries), hhmm)


if __name__ == "__main__":
    asyncio.run(main())
