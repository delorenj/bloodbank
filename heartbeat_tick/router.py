"""Heartbeat Router — single service that dispatches all agent heartbeat checks.

Consumes `system.heartbeat.tick` from Bloodbank. On each tick:
1. Scans all agent workspaces for heartbeat.json files
2. For each agent, calculates overdue checks
3. Dispatches overdue checks by injecting system events into the agent's
   OpenClaw session via the /hooks/agent endpoint

This replaces the per-agent sidecar model. One service, all agents.

Usage:
    python -m heartbeat_tick.router

Environment:
    RABBITMQ_URL            amqp connection string
    BLOODBANK_EXCHANGE      exchange name (default: bloodbank.events.v1)
    OPENCLAW_HOOK_URL       OpenClaw hooks endpoint (default: http://host.docker.internal:18789/hooks/agent)
    OPENCLAW_HOOK_TOKEN     Bearer token for hook auth (required)
    WORKSPACES_ROOT         Root dir containing workspace-{agent}/ dirs (default: /workspaces)
    LOG_LEVEL               logging level (default: INFO)
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import aio_pika
import httpx
from aio_pika import ExchangeType

from .schema import HeartbeatConfig

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [heartbeat-router] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "")
EXCHANGE_NAME = os.environ.get("BLOODBANK_EXCHANGE", "bloodbank.events.v1")
OPENCLAW_HOOK_URL = os.environ.get(
    "OPENCLAW_HOOK_URL", "http://host.docker.internal:18789/hooks/agent"
)
OPENCLAW_HOOK_TOKEN = os.environ.get("OPENCLAW_HOOK_TOKEN", "")
WORKSPACES_ROOT = Path(os.environ.get("WORKSPACES_ROOT", "/workspaces"))
HOSTNAME = socket.gethostname()

# State: {agent_name: {check_id: last_run_epoch}}
STATE_PATH = Path(os.environ.get("HEARTBEAT_STATE", "/state/heartbeat-router-state.json"))

# Map workspace dir names to agent session keys
# workspace-lenoon → agent:infra:main, workspace → agent:main:main, etc.
AGENT_SESSION_MAP: dict[str, str] = {
    "workspace": "agent:main:main",           # Cack
    "workspace-grolf": "agent:eng:main",
    "workspace-eng": "agent:eng:main",
    "workspace-lenoon": "agent:infra:main",
    "workspace-infra": "agent:infra:main",
    "workspace-rererere": "agent:work:main",
    "workspace-tonny": "agent:family:main",
    "workspace-rar": "agent:rar:main",
    "workspace-pepe": "agent:pepe:main",
    "workspace-overworld": "agent:overworld:main",
    "workspace-svgme": "agent:svgme:main",
    "workspace-wean": "agent:wean:main",
    "workspace-dumpling": "agent:dumpling:main",
    "workspace-cack-app": "agent:cack-app:main",
}


# ─── State management ────────────────────────────────────────────────────────


def _load_state() -> dict[str, dict[str, float]]:
    """Load {agent: {check_id: last_run_epoch}} from state file."""
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, dict[str, float]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _is_overdue(
    agent: str, check_id: str, interval_minutes: int, state: dict[str, dict[str, float]]
) -> bool:
    agent_state = state.get(agent, {})
    last = agent_state.get(check_id, 0)
    return (time.time() - last) >= (interval_minutes * 60)


# ─── Workspace scanning ──────────────────────────────────────────────────────


def scan_all_heartbeat_configs() -> list[tuple[str, str, HeartbeatConfig]]:
    """Scan workspace root for heartbeat.json files.

    Returns list of (workspace_dirname, session_key, config).
    """
    configs: list[tuple[str, str, HeartbeatConfig]] = []

    if not WORKSPACES_ROOT.exists():
        logger.warning("Workspaces root not found: %s", WORKSPACES_ROOT)
        return configs

    for d in sorted(WORKSPACES_ROOT.iterdir()):
        if not d.is_dir():
            continue
        if not d.name.startswith("workspace"):
            continue
        hb = d / "heartbeat.json"
        if not hb.exists():
            continue

        dirname = d.name
        session_key = AGENT_SESSION_MAP.get(dirname)
        if not session_key:
            # Try to derive: workspace-{name} → agent:{name}:main
            if dirname.startswith("workspace-"):
                agent_name = dirname[len("workspace-"):]
                session_key = f"agent:{agent_name}:main"
            else:
                logger.warning("No session mapping for %s, skipping", dirname)
                continue

        try:
            config = HeartbeatConfig.from_json(str(hb))
            configs.append((dirname, session_key, config))
            logger.debug(
                "Loaded %s: agent=%s, %d checks",
                dirname,
                config.agent,
                len(config.checks),
            )
        except Exception as e:
            logger.error("Failed to parse %s: %s", hb, e)

    return configs


# ─── Dispatch via OpenClaw hooks ──────────────────────────────────────────────


async def dispatch_check(
    agent_name: str,
    session_key: str,
    check_id: str,
    prompt: str,
    exchange: Any,
) -> bool:
    """Inject a heartbeat check prompt into an agent's OpenClaw session."""

    if not OPENCLAW_HOOK_TOKEN:
        logger.error("OPENCLAW_HOOK_TOKEN not set — cannot dispatch")
        return False

    # Build the message with heartbeat context
    message = (
        f"[Heartbeat Dispatch] check_id={check_id}\n\n"
        f"{prompt}"
    )

    payload = {
        "message": message,
        "name": f"HeartbeatRouter:{check_id}",
        "sessionKey": session_key,
        "wakeMode": "now",
        "deliver": False,
        "timeoutSeconds": 300,
    }

    headers = {
        "Authorization": f"Bearer {OPENCLAW_HOOK_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(OPENCLAW_HOOK_URL, headers=headers, json=payload)
            resp.raise_for_status()

        logger.info(
            "Dispatched check %s → %s (session=%s)",
            check_id,
            agent_name,
            session_key,
        )

        # Also publish dispatch event to Bloodbank for observability
        envelope = {
            "event_id": str(uuid4()),
            "event_type": f"agent.{agent_name}.heartbeat.dispatch",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": "1.0.0",
            "source": {
                "host": HOSTNAME,
                "type": "heartbeat",
                "app": "heartbeat-router",
            },
            "correlation_ids": [],
            "payload": {
                "agent": agent_name,
                "check_id": check_id,
                "session_key": session_key,
                "action": "system_event",
                "dispatched_at": datetime.now(timezone.utc).isoformat(),
            },
        }

        await exchange.publish(
            aio_pika.Message(
                body=json.dumps(envelope).encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=f"agent.{agent_name}.heartbeat.dispatch",
        )

        return True

    except httpx.HTTPStatusError as e:
        logger.error(
            "Hook dispatch failed for %s/%s: HTTP %d — %s",
            agent_name,
            check_id,
            e.response.status_code,
            e.response.text[:200],
        )
        return False
    except Exception as e:
        logger.error(
            "Hook dispatch error for %s/%s: %s",
            agent_name,
            check_id,
            e,
        )
        return False


# ─── Tick handler ─────────────────────────────────────────────────────────────


async def handle_tick(tick_payload: dict[str, Any], exchange: Any) -> None:
    """Process a heartbeat tick for ALL agents."""
    day = tick_payload.get("day_of_week", "")
    hour = tick_payload.get("hour", 0)
    quarter = tick_payload.get("quarter", "")
    tick_num = tick_payload.get("tick", 0)

    # Scan all heartbeat configs (hot-reload every tick)
    configs = scan_all_heartbeat_configs()
    if not configs:
        logger.debug("Tick #%d: no heartbeat.json files found", tick_num)
        return

    state = _load_state()
    total_dispatched = 0

    for dirname, session_key, config in configs:
        agent = config.agent

        for check in config.checks:
            if not check.enabled:
                continue

            # Check conditions (time of day, day of week, quarter)
            if not check.conditions.matches(day, hour, quarter):
                continue

            # Check if overdue
            if not _is_overdue(agent, check.id, check.interval_minutes, state):
                continue

            # Dispatch
            success = await dispatch_check(
                agent_name=agent,
                session_key=session_key,
                check_id=check.id,
                prompt=check.prompt,
                exchange=exchange,
            )

            if success:
                if agent not in state:
                    state[agent] = {}
                state[agent][check.id] = time.time()
                total_dispatched += 1

    _save_state(state)

    logger.info(
        "Tick #%d: scanned %d agents, dispatched %d checks",
        tick_num,
        len(configs),
        total_dispatched,
    )


# ─── Queue provisioning ──────────────────────────────────────────────────────


async def provision_agent_inboxes(channel: Any, exchange: Any) -> int:
    """Declare agent.{name}.inbox queues for all known agents, bound to agent.{name}.#.

    Returns number of queues provisioned.
    """
    agents = [
        "cack", "grolf", "lenoon", "rererere", "tonny", "tongy",
        "rar", "pepe", "lalathing", "momothecat", "yi",
        "overworld", "svgme", "wean", "dumpling", "cack-app",
    ]

    provisioned = 0
    for agent in agents:
        queue_name = f"agent.{agent}.inbox"
        routing_key = f"agent.{agent}.#"

        queue = await channel.declare_queue(queue_name, durable=True)
        await queue.bind(exchange, routing_key=routing_key)

        logger.info("Provisioned queue: %s → %s", queue_name, routing_key)
        provisioned += 1

    return provisioned


# ─── Main consumer loop ──────────────────────────────────────────────────────


async def run() -> None:
    if not RABBITMQ_URL:
        logger.error("RABBITMQ_URL not set")
        sys.exit(1)

    if not OPENCLAW_HOOK_TOKEN:
        logger.warning("OPENCLAW_HOOK_TOKEN not set — dispatches will fail")

    logger.info("Starting heartbeat router (workspaces=%s)", WORKSPACES_ROOT)

    # Connect to RabbitMQ
    connection = await aio_pika.connect_robust(RABBITMQ_URL, heartbeat=30)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=1)

    exchange = await channel.declare_exchange(
        EXCHANGE_NAME,
        ExchangeType.TOPIC,
        durable=True,
    )

    # Provision per-agent inbox queues
    n = await provision_agent_inboxes(channel, exchange)
    logger.info("Provisioned %d agent inbox queues", n)

    # Declare router's own queue — listens only to system.heartbeat.tick
    router_queue = await channel.declare_queue(
        "heartbeat-router",
        durable=True,
    )
    await router_queue.bind(exchange, routing_key="system.heartbeat.tick")

    logger.info("Router queue bound to system.heartbeat.tick")

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
                await handle_tick(payload, exchange)
            else:
                logger.debug("Ignoring non-tick event: %s", event_type)

            await message.ack()
        except Exception as e:
            logger.error("Error processing tick: %s", e, exc_info=True)
            try:
                await message.reject(requeue=False)
            except Exception:
                pass

    await router_queue.consume(_process)
    logger.info("Heartbeat router consuming — waiting for ticks...")

    # Wait for shutdown
    await shutdown.wait()
    await connection.close()
    logger.info("Heartbeat router shut down")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
