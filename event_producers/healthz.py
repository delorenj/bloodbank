"""Shared /healthz HTTP endpoint for Bloodbank consumers.

Starts a lightweight aiohttp server as a background asyncio task.
Exposes GET /healthz that checks AMQP connection liveness.

Usage:
    from event_producers.healthz import start_healthz_server

    # Pass a callable that returns True/False for AMQP connection status
    await start_healthz_server(lambda: conn is not None and not conn.is_closed)

Environment:
    HEALTHZ_PORT   HTTP port to listen on (default: 18690)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable

from aiohttp import web

logger = logging.getLogger(__name__)

DEFAULT_PORT = 18690


async def _handle_healthz(request: web.Request) -> web.Response:
    """GET /healthz handler."""
    check_fn: Callable[[], bool] = request.app["amqp_check"]
    connected = False
    try:
        connected = check_fn()
    except Exception:
        connected = False

    healthy = connected
    body = {
        "status": "ok" if healthy else "degraded",
        "amqp_connected": connected,
    }
    return web.json_response(body, status=200 if healthy else 503)


async def start_healthz_server(
    amqp_check: Callable[[], bool],
    port: int | None = None,
    host: str = "0.0.0.0",
) -> asyncio.Task:
    """Start a background healthz HTTP server.

    Args:
        amqp_check: Callable returning True when the AMQP connection is alive.
        port: Port to listen on (default: HEALTHZ_PORT env or 18690).
        host: Bind address (default: 0.0.0.0).

    Returns:
        The asyncio.Task running the server (can be cancelled for shutdown).
    """
    if port is None:
        port = int(os.environ.get("HEALTHZ_PORT", str(DEFAULT_PORT)))

    app = web.Application()
    app["amqp_check"] = amqp_check
    app.router.add_get("/healthz", _handle_healthz)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Healthz server listening on %s:%d", host, port)

    async def _serve_forever() -> None:
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await runner.cleanup()
            raise

    task = asyncio.create_task(_serve_forever())
    return task
