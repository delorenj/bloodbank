"""Lifecycle controller service entrypoint.

Runs three concurrent loops:
1. ReconcileWorker — claims dirty lifecycles, evaluates state, persists
2. Sweeper — periodic backstop enqueue of all active lifecycles
3. OutboxPublisher — polls outbox table, publishes to Bloodbank

Usage:
    python -m lifecycle_controller.main

Environment:
    DATABASE_URL — postgres connection string (default: postgresql://candystore:candystore@localhost:5432/candystore)
    WORKER_INTERVAL — seconds between worker polls (default: 5)
    SWEEP_INTERVAL — seconds between sweeps (default: 300)
    OUTBOX_INTERVAL — seconds between outbox polls (default: 5)
"""
from __future__ import annotations

import asyncio
import os

import asyncpg
import structlog

from db.repository import LifecycleRepository
from outbox_publisher import OutboxPublisher
from sweeper import Sweeper
from worker import ReconcileWorker

logger = structlog.get_logger()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://candystore:candystore@localhost:5432/candystore")
WORKER_INTERVAL = float(os.getenv("WORKER_INTERVAL", "5"))
SWEEP_INTERVAL = float(os.getenv("SWEEP_INTERVAL", "300"))
OUTBOX_INTERVAL = float(os.getenv("OUTBOX_INTERVAL", "5"))


def _redact_database_url(database_url: str) -> str:
    if "://" not in database_url:
        return database_url
    scheme, rest = database_url.split("://", 1)
    if "@" not in rest:
        return database_url
    _, host_and_path = rest.rsplit("@", 1)
    return f"{scheme}://***@{host_and_path}"


async def main() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    log = logger.bind(service="lifecycle-controller")
    log.info("startup", database_url=_redact_database_url(DATABASE_URL))

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    repo = LifecycleRepository(pool)

    worker = ReconcileWorker(repo)
    sweeper = Sweeper(repo)
    publisher = OutboxPublisher(repo)

    await asyncio.gather(
        worker.run_loop(interval_seconds=WORKER_INTERVAL),
        sweeper.run_loop(interval_seconds=SWEEP_INTERVAL),
        publisher.run_loop(interval_seconds=OUTBOX_INTERVAL),
    )


if __name__ == "__main__":
    asyncio.run(main())
