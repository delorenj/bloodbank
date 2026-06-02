"""Transactional outbox publisher.

Polls the outbox table and publishes events to Bloodbank (NATS/Dapr).
Separates DB writes from network calls so the reconciler stays fast.
"""
from __future__ import annotations

import asyncio

import structlog

from db.repository import LifecycleRepository
from models import OutboxEvent

logger = structlog.get_logger()


class OutboxPublisher:
    def __init__(self, repo: LifecycleRepository, publish_fn = None) -> None:
        self.repo = repo
        self.publish_fn = publish_fn or self._default_publish

    async def _default_publish(self, event: OutboxEvent) -> None:
        """Placeholder: wire to NATS/Dapr publisher when available."""
        logger.warning(
            "outbox_publish_not_configured",
            event_type=event.event_type,
            lifecycle_id=event.lifecycle_id,
        )
        raise RuntimeError("outbox publisher is not configured")
        # TODO: integrate with bloodbank publisher
        # await nats_publish(
        #     subject=f"bloodbank.evt.v1.{event.event_type}",
        #     payload=event.payload,
        #     headers=event.headers,
        # )

    async def run_once(self, batch_size: int = 100) -> int:
        """Publish one batch of unpublished outbox events. Returns count published."""
        events = await self.repo.get_unpublished_outbox(batch_size)
        published = 0
        for event in events:
            if event.id is None:
                continue
            try:
                await self.publish_fn(event)
                await self.repo.mark_outbox_published(event.id)
                published += 1
            except Exception as exc:
                logger.warning("outbox_publish_failed", outbox_id=event.id, error=str(exc))
                await self.repo.mark_outbox_failed(event.id, str(exc))
        return published

    async def run_loop(self, interval_seconds: float = 5.0) -> None:
        """Continuously poll and publish."""
        while True:
            count = await self.run_once()
            if count == 0:
                await asyncio.sleep(interval_seconds)
