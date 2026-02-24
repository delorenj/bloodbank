"""
Entry point: python -m hookd_bridge

Runs the hookd compatibility bridge as a standalone HTTP server.
"""
import logging
import os

from aiohttp import web

from .bridge import create_app, BRIDGE_HOST, BRIDGE_PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("hookd-bridge")


def main() -> None:
    logger.info("hookd Compatibility Bridge (GOD-4 / MIG-1)")
    logger.info(f"  Listen: {BRIDGE_HOST}:{BRIDGE_PORT}")
    logger.info(f"  Auth: {'token' if os.environ.get('BRIDGE_TOKEN') else 'none'}")

    app = create_app()
    web.run_app(app, host=BRIDGE_HOST, port=BRIDGE_PORT, print=None)


if __name__ == "__main__":
    main()
