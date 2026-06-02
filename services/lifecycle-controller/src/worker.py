"""Reconcile worker.

Claims a lifecycle from the dirty queue, runs reconciliation, persists
results transactionally, and releases the lease.
"""
from __future__ import annotations

import asyncio
import uuid

import structlog

from db.repository import LifecycleRepository
from reconciler import reconcile

logger = structlog.get_logger()


class ReconcileWorker:
    def __init__(self, repo: LifecycleRepository, worker_id: str | None = None) -> None:
        self.repo = repo
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"

    async def run_once(self) -> bool:
        """Claim one job, reconcile, persist, release. Returns True if work was done."""
        lifecycle_id = await self.repo.claim_next_reconcile_job(
            worker_id=self.worker_id, lease_seconds=60
        )
        if not lifecycle_id:
            return False

        log = logger.bind(lifecycle_id=lifecycle_id, worker=self.worker_id)
        log.info("reconcile_start")

        try:
            # Gather inputs
            previous_state = await self.repo.get_lifecycle_state(lifecycle_id)
            observations = await self.repo.get_recent_observations(lifecycle_id)
            active_blockers = await self.repo.get_active_blockers(lifecycle_id)
            active_gates = await self.repo.get_active_gates(lifecycle_id)
            checkpoints = await self.repo.get_checkpoints(lifecycle_id)
            sentinel_health = await self.repo.get_sentinel_health()

            # Run reconciler
            result = reconcile(
                lifecycle_id=lifecycle_id,
                previous_state=previous_state,
                observations=observations,
                active_blockers=active_blockers,
                active_gates=active_gates,
                checkpoints=checkpoints,
                sentinel_health=sentinel_health,
            )

            # Persist atomically
            await self.repo.persist_reconcile_result(
                lifecycle_id=lifecycle_id,
                state=result.current_state,
                outbox_events=result.outbox_events,
            )

            if result.state_changed:
                log.info(
                    "reconcile_state_changed",
                    status=result.current_state.status.value,
                    health=result.current_state.health.value,
                    reason=result.current_state.status_reason,
                )
            else:
                log.info("reconcile_no_change")

            await self.repo.delete_reconcile_job(lifecycle_id)

            return True

        except Exception:
            log.exception("reconcile_failed")
            await self.repo.release_lease(lifecycle_id, requeue_delay_seconds=30)
            return True

    async def run_loop(self, interval_seconds: float = 5.0) -> None:
        """Continuously claim and process jobs."""
        while True:
            worked = await self.run_once()
            if not worked:
                await asyncio.sleep(interval_seconds)
