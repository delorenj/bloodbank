"""Local Heartbeat Scheduler — self-ticking, no global tick dependency.

Replaces the centralized heartbeat-tick publisher + heartbeat-router consumer
architecture with a single self-scheduling service.

Key changes from the old architecture:
- NO dependency on `system.heartbeat.tick` from RabbitMQ
- Internal asyncio timer drives the tick loop (configurable interval)
- Still publishes `agent.{name}.heartbeat.dispatch` events to Bloodbank for observability
- Still dispatches checks via OpenClaw /hooks/agent endpoint
- Scans all agent workspaces for heartbeat.json on each tick (hot-reload)

Usage:
    python -m heartbeat_tick.local_scheduler

Environment:
    RABBITMQ_URL            amqp connection string (for publishing dispatch events)
    BLOODBANK_EXCHANGE      exchange name (default: bloodbank.events.v1)
    OPENCLAW_HOOK_URL       OpenClaw hooks endpoint (default: http://127.0.0.1:18790/hooks/agent)
    OPENCLAW_HOOK_TOKEN     Bearer token for hook auth (required)
    WORKSPACES_ROOT         Root dir containing workspace-{agent}/ dirs (default: ~/.openclaw)
    TICK_INTERVAL_S         seconds between ticks (default: 60)
    LOG_LEVEL               logging level (default: INFO)

Ticket: BB-3
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

from event_producers.healthz import start_healthz_server
from .schema import HeartbeatConfig

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [heartbeat-local] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "")
EXCHANGE_NAME = os.environ.get("BLOODBANK_EXCHANGE", "bloodbank.events.v1")
OPENCLAW_HOOK_URL = os.environ.get(
    "OPENCLAW_HOOK_URL", "http://127.0.0.1:18790/hooks/agent"
)
OPENCLAW_HOOK_TOKEN = os.environ.get("OPENCLAW_HOOK_TOKEN", "")
WORKSPACES_ROOT = Path(
    os.environ.get("WORKSPACES_ROOT", os.path.expanduser("~/.openclaw"))
)
TICK_INTERVAL = int(os.environ.get("TICK_INTERVAL_S", "60"))
HOSTNAME = socket.gethostname()

# State: {agent_name: {check_id: last_run_epoch}}
STATE_PATH = Path(
    os.environ.get("HEARTBEAT_STATE", "/tmp/heartbeat-local-state.json")
)

# Map workspace dir names to agent session keys
AGENT_SESSION_MAP: dict[str, str] = {
    "workspace": "agent:main:main",
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
    "workspace-momo": "agent:svgme:main",
    "workspace-tongy": "agent:wean:main",
}


# ─── State management ────────────────────────────────────────────────────────


def _load_state() -> dict[str, dict[str, float]]:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, dict[str, float]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _is_overdue(
    agent: str,
    check_id: str,
    interval_minutes: int,
    state: dict[str, dict[str, float]],
) -> bool:
    agent_state = state.get(agent, {})
    last = agent_state.get(check_id, 0)
    return (time.time() - last) >= (interval_minutes * 60)


# ─── Tick payload builder ────────────────────────────────────────────────────


def _quarter(month: int) -> str:
    return f"Q{(month - 1) // 3 + 1}"


def _build_tick_payload(tick: int) -> dict[str, Any]:
    """Build tick context — same shape as old publisher for compatibility."""
    now = datetime.now(timezone.utc)
    return {
        "tick": tick,
        "timestamp": now.isoformat(),
        "epoch_ms": int(now.timestamp() * 1000),
        "quarter": _quarter(now.month),
        "day_of_week": now.strftime("%A"),
        "hour": now.hour,
        "minute": now.minute,
    }


# ─── Workspace scanning ──────────────────────────────────────────────────────


def scan_all_heartbeat_configs() -> list[tuple[str, str, HeartbeatConfig]]:
    """Scan workspace root for heartbeat.json files.

    Supports two schemas:
    1. Array format: {agent, checks: [{id, interval_minutes, enabled, ...}]}
    2. Dict format:  {agent, checks: {check_id: {cadenceMs, ...}}}

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
            if dirname.startswith("workspace-"):
                agent_name = dirname[len("workspace-"):]
                session_key = f"agent:{agent_name}:main"
            else:
                logger.warning("No session mapping for %s, skipping", dirname)
                continue

        try:
            raw = json.loads(hb.read_text())
            agent = raw.get("agent", dirname)
            checks_raw = raw.get("checks", [])

            # Normalize both formats to list of dicts
            if isinstance(checks_raw, dict):
                checks = []
                for check_id, check_data in checks_raw.items():
                    cadence_ms = check_data.get("cadenceMs", 0)
                    window = check_data.get("window", {})
                    checks.append({
                        "id": check_id,
                        "interval_minutes": max(1, cadence_ms // 60000),
                        "enabled": check_data.get("enabled", True),
                        "prompt": check_data.get("description", f"Check {check_id}"),
                        "action": "system_event",
                        "conditions": {
                            "day_of_week": [],
                            "hour_range": [0, 23],
                        },
                    })
            else:
                checks = checks_raw

            config = HeartbeatConfig(agent=agent, checks=checks)
            configs.append((dirname, session_key, config))
            logger.debug("Loaded %s: agent=%s, %d checks", dirname, agent, len(checks))
        except Exception as e:
            logger.error("Failed to parse %s: %s", hb, e)

    return configs


# ─── Dispatch via OpenClaw hooks ──────────────────────────────────────────────


async def dispatch_check(
    agent_name: str,
    session_key: str,
    check_id: str,
    prompt: str,
    exchange: Any | None,
) -> bool:
    """Inject a heartbeat check prompt into an agent's OpenClaw session."""

    if not OPENCLAW_HOOK_TOKEN:
        logger.error("OPENCLAW_HOOK_TOKEN not set — cannot dispatch")
        return False

    message = f"[Heartbeat Dispatch] check_id={check_id}\n\n{prompt}"

    payload = {
        "text": message,
        "name": f"HeartbeatScheduler:{check_id}",
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

        # Publish dispatch event to Bloodbank for observability (best-effort)
        if exchange is not None:
            try:
                envelope = {
                    "event_id": str(uuid4()),
                    "event_type": f"agent.{agent_name}.heartbeat.dispatch",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "version": "1.0.0",
                    "source": {
                        "host": HOSTNAME,
                        "type": "heartbeat",
                        "app": "heartbeat-local-scheduler",
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
            except Exception as e:
                # Best-effort — don't fail the dispatch if Bloodbank publish fails
                logger.warning("Failed to publish dispatch event to Bloodbank: %s", e)

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
        logger.error("Hook dispatch error for %s/%s: %s", agent_name, check_id, e)
        return False


# ─── Tick handler ─────────────────────────────────────────────────────────────


async def handle_tick(tick_payload: dict[str, Any], exchange: Any | None) -> None:
    """Process a tick for ALL agents — scan configs, dispatch overdue checks."""
    day = tick_payload.get("day_of_week", "")
    hour = tick_payload.get("hour", 0)
    quarter = tick_payload.get("quarter", "")
    tick_num = tick_payload.get("tick", 0)

    configs = scan_all_heartbeat_configs()
    if not configs:
        logger.debug("Tick #%d: no heartbeat.json files found", tick_num)
        return

    state = _load_state()
    total_dispatched = 0

    for dirname, session_key, config in configs:
        agent = config.agent

        for check in config.checks:
            # Handle both dict and object formats
            check_id = check.get("id") if isinstance(check, dict) else check.id
            enabled = (
                check.get("enabled", True)
                if isinstance(check, dict)
                else check.enabled
            )
            interval_minutes = (
                check.get("interval_minutes", 60)
                if isinstance(check, dict)
                else check.interval_minutes
            )
            prompt = (
                check.get("prompt", f"Check {check_id}")
                if isinstance(check, dict)
                else check.prompt
            )

            if not enabled:
                continue

            # Check conditions
            conditions = (
                check.get("conditions", {})
                if isinstance(check, dict)
                else check.conditions
            )
            if isinstance(conditions, dict):
                cond_days = conditions.get("day_of_week", [])
                cond_hours = conditions.get("hour_range", [])
                if cond_days and day not in cond_days:
                    continue
                if cond_hours and len(cond_hours) == 2:
                    start, end = cond_hours
                    if start <= end:
                        if not (start <= hour <= end):
                            continue
                    else:
                        # Wrap-around (e.g., [11, 4] means 11→midnight→4)
                        if not (hour >= start or hour <= end):
                            continue
            elif hasattr(conditions, "matches"):
                if not conditions.matches(day, hour, quarter):
                    continue

            # Check if overdue
            if not _is_overdue(agent, check_id, interval_minutes, state):
                continue

            # Dispatch
            success = await dispatch_check(
                agent_name=agent,
                session_key=session_key,
                check_id=check_id,
                prompt=prompt,
                exchange=exchange,
            )

            if success:
                state.setdefault(agent, {})[check_id] = time.time()
                total_dispatched += 1

    _save_state(state)

    logger.info(
        "Tick #%d: scanned %d agents, dispatched %d checks",
        tick_num,
        len(configs),
        total_dispatched,
    )


# ─── Main loop — self-scheduling ─────────────────────────────────────────────


async def run() -> None:
    if not OPENCLAW_HOOK_TOKEN:
        logger.warning("OPENCLAW_HOOK_TOKEN not set — dispatches will fail")

    logger.info(
        "Starting local heartbeat scheduler: interval=%ds, workspaces=%s",
        TICK_INTERVAL,
        WORKSPACES_ROOT,
    )

    # Connect to RabbitMQ for publishing dispatch events (best-effort)
    exchange = None
    connection = None
    if RABBITMQ_URL:
        try:
            connection = await aio_pika.connect_robust(RABBITMQ_URL, heartbeat=30)
            channel = await connection.channel()
            exchange = await channel.declare_exchange(
                EXCHANGE_NAME,
                ExchangeType.TOPIC,
                durable=True,
            )
            logger.info("Connected to RabbitMQ for dispatch event publishing")
        except Exception as e:
            logger.warning(
                "RabbitMQ connection failed (dispatch events will be skipped): %s", e
            )
            exchange = None
    else:
        logger.info(
            "RABBITMQ_URL not set — running without Bloodbank event publishing"
        )

    # Start /healthz endpoint
    healthz_task = await start_healthz_server(
        lambda: connection is not None and not connection.is_closed
    )

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
            tick_payload = _build_tick_payload(tick)

            try:
                await handle_tick(tick_payload, exchange)
            except Exception as e:
                logger.error("Tick #%d failed: %s", tick, e, exc_info=True)

            # Wait for interval or shutdown
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=TICK_INTERVAL)
            except asyncio.TimeoutError:
                pass  # Normal — interval elapsed, continue
    finally:
        healthz_task.cancel()
        try:
            await healthz_task
        except asyncio.CancelledError:
            pass
        if connection:
            try:
                await connection.close()
            except Exception:
                pass
        logger.info("Local scheduler shut down after %d ticks", tick)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
