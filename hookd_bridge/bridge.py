"""
hookd Bridge — HTTP server that translates hook calls to Bloodbank commands.

Endpoint: POST /hooks/agent
  Body: { "text": "...", "sessionKey": "agent:{name}:main" }

The bridge:
1. Parses the session key to extract agent name
2. Infers action from the text (e.g., "[Command] action=run_drift_check ...")
   or falls back to "hook_dispatch"
3. Wraps in a CommandEnvelope
4. Publishes to command.{agent}.{action} on Bloodbank
5. Returns 202 Accepted
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import aio_pika
import orjson
from aiohttp import web

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RABBITMQ_URL = os.environ.get(
    "RABBITMQ_URL",
    os.environ.get("RABBIT_URL", "amqp://delorenj:MISSING_PASSWORD@127.0.0.1:5673/"),
)
EXCHANGE_NAME = os.environ.get("EXCHANGE_NAME", "bloodbank.events.v1")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "18790"))
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")  # Optional auth token
DEFAULT_TTL_MS = int(os.environ.get("DEFAULT_TTL_MS", "30000"))

# Regex to parse structured command text: [Command] action=X id=Y from=Z priority=P
COMMAND_RE = re.compile(
    r"\[Command\]\s+"
    r"action=(?P<action>\S+)"
    r"(?:\s+id=(?P<id>\S+))?"
    r"(?:\s+from=(?P<from>\S+))?"
    r"(?:\s+priority=(?P<priority>\S+))?"
)

# Canonical action for repository hygiene/maintenance work.
GIT_MAINTENANCE_ACTION = "run_git_maintenance"

# Action aliases normalized by normalize_action() (lowercase, punctuation->underscore).
ACTION_ALIASES: dict[str, str] = {
    "git_maintenance": GIT_MAINTENANCE_ACTION,
    "gitmaintenance": GIT_MAINTENANCE_ACTION,
    "git_maint": GIT_MAINTENANCE_ACTION,
    "run_git_maintenance": GIT_MAINTENANCE_ACTION,
}

# Regex to extract agent name from sessionKey: agent:{name}:main
SESSION_KEY_RE = re.compile(r"^agent:(?P<agent>[^:]+):")


def normalize_action(action: str) -> str:
    """Normalize action names and collapse known aliases to canonical actions."""
    normalized = action.strip().lower().replace("-", "_").replace(".", "_").replace(" ", "_")
    if not normalized:
        return "hook_dispatch"
    return ACTION_ALIASES.get(normalized, normalized)


# ---------------------------------------------------------------------------
# Command envelope builder
# ---------------------------------------------------------------------------
def build_command_envelope(
    *,
    target_agent: str,
    action: str,
    issued_by: str = "hookd-bridge",
    priority: str = "normal",
    command_payload: dict[str, Any] | None = None,
    ttl_ms: int = DEFAULT_TTL_MS,
    correlation_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Build a CommandEnvelope and return (routing_key, envelope_dict).
    """
    command_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    corr_id = correlation_id or str(uuid.uuid4())

    envelope = {
        "event_id": str(uuid.uuid4()),
        "event_type": "command.envelope",
        "timestamp": now,
        "version": "1.0.0",
        # Include both `type` and `trigger_type` for compatibility across
        # current/legacy consumers and schema validators.
        "source": {
            "host": os.uname().nodename,
            "app": "hookd-bridge",
            "type": "webhook",
            "trigger_type": "webhook",
        },
        "correlation_id": corr_id,
        "correlation_ids": [corr_id],
        "payload": {
            "command_id": command_id,
            "target_agent": target_agent,
            "issued_by": issued_by,
            "action": action,
            "priority": priority,
            "ttl_ms": ttl_ms,
            "idempotency_key": None,
            "command_payload": command_payload or {},
        },
    }

    routing_key = f"command.{target_agent}.{action}"
    return routing_key, envelope


def parse_hook_text(text: str) -> tuple[str, str, str, dict[str, Any]]:
    """
    Parse hook text to extract action, issued_by, priority, and remaining payload.

    Returns (action, issued_by, priority, extra_payload).
    """
    m = COMMAND_RE.search(text)
    if m:
        action = normalize_action(m.group("action") or "hook_dispatch")
        issued_by = m.group("from") or "hookd-bridge"
        priority = m.group("priority") or "normal"
        # Everything after the [Command] line is payload
        remaining = text[m.end():].strip()
        extra: dict[str, Any] = {}
        if remaining:
            try:
                extra = orjson.loads(remaining)
            except Exception:
                extra = {"raw_text": remaining}
        return action, issued_by, priority, extra

    # Lightweight shorthand support for direct git maintenance requests.
    if normalize_action(text) in ACTION_ALIASES:
        return GIT_MAINTENANCE_ACTION, "hookd-bridge", "normal", {"raw_text": text}

    # No structured command — treat entire text as a generic dispatch
    return "hook_dispatch", "hookd-bridge", "normal", {"raw_text": text}


def is_empty_noop(text: str, action: str, command_payload: dict[str, Any]) -> bool:
    """Return True when a hook contains no actionable content and should be dropped."""
    if text.strip():
        return False

    if action != "hook_dispatch":
        return False

    raw_text = command_payload.get("raw_text")
    return isinstance(raw_text, str) and not raw_text.strip()


def extract_agent_from_session_key(session_key: str) -> str | None:
    """Extract agent name from sessionKey like 'agent:lenoon:main'."""
    m = SESSION_KEY_RE.match(session_key)
    return m.group("agent") if m else None


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class HookdBridge:
    """aiohttp-based HTTP bridge server."""

    def __init__(self) -> None:
        self._conn: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None
        self._exchange: aio_pika.Exchange | None = None
        self._stats = {"received": 0, "published": 0, "errors": 0}

    async def setup(self, app: web.Application) -> None:
        """Connect to RabbitMQ on app startup."""
        logger.info(f"Connecting to RabbitMQ: {RABBITMQ_URL.split('@')[1] if '@' in RABBITMQ_URL else RABBITMQ_URL}")
        self._conn = await aio_pika.connect_robust(RABBITMQ_URL, heartbeat=30)
        self._channel = await self._conn.channel()
        self._exchange = await self._channel.declare_exchange(
            EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
        )
        logger.info(f"Connected to exchange {EXCHANGE_NAME}")

    async def cleanup(self, app: web.Application) -> None:
        """Close RabbitMQ on app shutdown."""
        if self._conn:
            await self._conn.close()

    async def handle_hook(self, request: web.Request) -> web.Response:
        """
        POST /hooks/agent
        Body: { "text": "...", "sessionKey": "agent:{name}:main" }
        """
        self._stats["received"] += 1

        # Optional token auth
        if BRIDGE_TOKEN:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {BRIDGE_TOKEN}":
                return web.json_response(
                    {"error": "unauthorized"}, status=401
                )

        try:
            body = await request.json()
        except Exception:
            self._stats["errors"] += 1
            return web.json_response(
                {"error": "invalid JSON body"}, status=400
            )

        text = body.get("text", "")
        if not isinstance(text, str):
            text = "" if text is None else str(text)
        session_key = body.get("sessionKey", "")

        # Extract agent name
        agent_name = extract_agent_from_session_key(session_key)
        if not agent_name:
            self._stats["errors"] += 1
            return web.json_response(
                {"error": f"Cannot extract agent from sessionKey: {session_key}"},
                status=400,
            )

        # Parse text into command fields
        action, issued_by, priority, command_payload = parse_hook_text(text)

        # Ignore no-op hook chatter (empty payloads).
        if is_empty_noop(text, action, command_payload):
            logger.info("Bridge: ignored empty no-op hook payload")
            return web.Response(status=204)

        # Extract correlation_id from headers if present
        correlation_id = request.headers.get("X-Correlation-Id")

        # Build and publish
        routing_key, envelope = build_command_envelope(
            target_agent=agent_name,
            action=action,
            issued_by=issued_by,
            priority=priority,
            command_payload=command_payload,
            correlation_id=correlation_id,
        )

        try:
            msg = aio_pika.Message(
                body=orjson.dumps(envelope),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            )
            await self._exchange.publish(msg, routing_key=routing_key)
            self._stats["published"] += 1

            logger.info(
                f"Bridge: {agent_name}.{action} "
                f"(from={issued_by}, priority={priority}, "
                f"id={envelope['payload']['command_id'][:8]})"
            )

            return web.json_response(
                {
                    "status": "accepted",
                    "command_id": envelope["payload"]["command_id"],
                    "routing_key": routing_key,
                },
                status=202,
            )

        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"Failed to publish command: {e}")
            return web.json_response(
                {"error": f"publish failed: {e}"}, status=502
            )

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /healthz"""
        connected = self._conn is not None and not self._conn.is_closed
        return web.json_response(
            {
                "status": "ok" if connected else "degraded",
                "rabbitmq_connected": connected,
                "stats": self._stats,
            },
            status=200 if connected else 503,
        )


def create_app() -> web.Application:
    """Create the aiohttp application."""
    bridge = HookdBridge()
    app = web.Application()
    app.on_startup.append(bridge.setup)
    app.on_cleanup.append(bridge.cleanup)
    app.router.add_post("/hooks/agent", bridge.handle_hook)
    app.router.add_get("/healthz", bridge.handle_health)
    return app
