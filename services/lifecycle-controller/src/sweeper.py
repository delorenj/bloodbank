"""Periodic sweeper — enqueues all active lifecycles for reconciliation.

This is the backstop for silence/time-based failures. Even if no sentinel
emits observations, the sweeper ensures lifecycles are re-evaluated on
cadence so staleness (stalled, blocked, degraded observers) is detected.
"""
from __future__ import annotations

import asyncio

import structlog

from db.repository import LifecycleRepository

logger = structlog.get_logger()


class Sweeper:
    def __init__(self, repo: LifecycleRepository) -> None:
        self.repo = repo

    async def run_once(self) -> int:
        """Enqueue all active lifecycles. Returns count enqueued."""
        count = await self.repo.enqueue_sweep()
        logger.info("sweep_enqueued", count=count)
        return count

    async def run_loop(self, interval_seconds: float = 300.0) -> None:
        """Run sweep every N seconds."""
        while True:
            await self.run_once()
            await asyncio.sleep(interval_seconds)
