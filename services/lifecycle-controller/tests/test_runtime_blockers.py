"""Regression tests for lifecycle-controller runtime blockers."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from db.repository import LifecycleRepository, _row_to_state
from main import _redact_database_url
from models import LifecycleHealth, LifecycleState, LifecycleStatus, OutboxEvent
from outbox_publisher import OutboxPublisher
import worker as worker_module
from worker import ReconcileWorker


def _compact(sql: str) -> str:
    return " ".join(sql.split())


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeAcquire:
    def __init__(self, conn: "_FakeConnection") -> None:
        self.conn = conn

    async def __aenter__(self) -> "_FakeConnection":
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self) -> None:
        self.fetchrow_sql = ""
        self.execute_sql = ""
        self.execute_args = ()

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def fetchrow(self, sql: str):
        self.fetchrow_sql = sql
        return {"lifecycle_id": "lc_1"}

    async def execute(self, sql: str, *args):
        self.execute_sql = sql
        self.execute_args = args


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConnection()
        self.execute_sql = ""
        self.execute_args = ()
        self.fetch_sql = ""
        self.fetch_args = ()

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.conn)

    async def execute(self, sql: str, *args):
        self.execute_sql = sql
        self.execute_args = args

    async def fetch(self, sql: str, *args):
        self.fetch_sql = sql
        self.fetch_args = args
        return []


@pytest.mark.asyncio
async def test_claim_next_reconcile_job_uses_safe_interval_math_and_expired_leases():
    pool = _FakePool()
    repo = LifecycleRepository(pool)

    lifecycle_id = await repo.claim_next_reconcile_job("worker-a", lease_seconds=42)

    claim_sql = _compact(pool.conn.fetchrow_sql)
    update_sql = _compact(pool.conn.execute_sql)
    assert lifecycle_id == "lc_1"
    assert "lease_expires_at <= now()" in claim_sql
    assert "interval '$" not in update_sql
    assert "$2::int * interval '1 second'" in update_sql
    assert pool.conn.execute_args == ("worker-a", 42, "lc_1")


@pytest.mark.asyncio
async def test_release_and_stale_sentinel_queries_use_safe_interval_math():
    pool = _FakePool()
    repo = LifecycleRepository(pool)

    await repo.release_lease("lc_1", requeue_delay_seconds=30)
    release_sql = _compact(pool.execute_sql)
    assert "interval '$" not in release_sql
    assert "$1::int * interval '1 second'" in release_sql
    assert pool.execute_args == (30, "lc_1")

    await repo.get_stale_sentinels(threshold_minutes=15)
    stale_sql = _compact(pool.fetch_sql)
    assert "interval '$" not in stale_sql
    assert "$1::int * interval '1 minute'" in stale_sql
    assert pool.fetch_args == (15,)


def test_row_to_state_decodes_jsonb_policy_string():
    state = _row_to_state(
        {
            "lifecycle_id": "lc_1",
            "status": "active",
            "health": "nominal",
            "phase": None,
            "progress_percent": 0,
            "roadmap_version": 1,
            "status_reason": "",
            "health_reason": "",
            "last_progress_at": None,
            "last_reconciled_at": None,
            "state_version": 1,
            "state_fingerprint": "",
            "policy": '{"progress_expected": false, "stalled_after_minutes": 12}',
        }
    )

    assert state.policy.progress_expected is False
    assert state.policy.stalled_after_minutes == 12


@pytest.mark.parametrize("state_changed", [True, False])
@pytest.mark.asyncio
async def test_successful_reconcile_deletes_queue_job(monkeypatch, state_changed: bool):
    class FakeRepo:
        def __init__(self) -> None:
            self.deleted = []
            self.released = []

        async def claim_next_reconcile_job(self, worker_id: str, lease_seconds: int = 60):
            return "lc_1"

        async def get_lifecycle_state(self, lifecycle_id: str):
            return LifecycleState(
                lifecycle_id=lifecycle_id,
                status=LifecycleStatus.ACTIVE,
                health=LifecycleHealth.NOMINAL,
            )

        async def get_recent_observations(self, lifecycle_id: str):
            return []

        async def get_active_blockers(self, lifecycle_id: str):
            return []

        async def get_active_gates(self, lifecycle_id: str):
            return []

        async def get_checkpoints(self, lifecycle_id: str):
            return []

        async def get_sentinel_health(self):
            return {}

        async def persist_reconcile_result(self, lifecycle_id, state, outbox_events):
            return None

        async def delete_reconcile_job(self, lifecycle_id: str):
            self.deleted.append(lifecycle_id)

        async def release_lease(self, lifecycle_id: str, requeue_delay_seconds: int = 0):
            self.released.append((lifecycle_id, requeue_delay_seconds))

    current_state = LifecycleState(
        lifecycle_id="lc_1",
        status=LifecycleStatus.ACTIVE,
        health=LifecycleHealth.NOMINAL,
    )

    def fake_reconcile(**kwargs):
        return SimpleNamespace(
            current_state=current_state,
            outbox_events=[],
            state_changed=state_changed,
        )

    repo = FakeRepo()
    monkeypatch.setattr(worker_module, "reconcile", fake_reconcile)

    worked = await ReconcileWorker(repo, worker_id="worker-a").run_once()

    assert worked is True
    assert repo.deleted == ["lc_1"]
    assert repo.released == []


@pytest.mark.asyncio
async def test_default_outbox_publish_keeps_event_unpublished():
    class FakeRepo:
        def __init__(self) -> None:
            self.published = []
            self.failed = []

        async def get_unpublished_outbox(self, batch_size: int):
            return [
                OutboxEvent(
                    id=123,
                    lifecycle_id="lc_1",
                    event_type="bloodbank.v1.lifecycle.status.updated",
                )
            ]

        async def mark_outbox_published(self, outbox_id: int):
            self.published.append(outbox_id)

        async def mark_outbox_failed(self, outbox_id: int, error: str):
            self.failed.append((outbox_id, error))

    repo = FakeRepo()
    published_count = await OutboxPublisher(repo).run_once()

    assert published_count == 0
    assert repo.published == []
    assert repo.failed == [(123, "outbox publisher is not configured")]


def test_redact_database_url_credentials():
    assert (
        _redact_database_url("postgresql://user:secret@localhost:5432/candystore")
        == "postgresql://***@localhost:5432/candystore"
    )
    assert _redact_database_url("postgresql://localhost:5432/candystore") == (
        "postgresql://localhost:5432/candystore"
    )
