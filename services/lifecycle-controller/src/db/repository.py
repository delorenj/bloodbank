"""DB repository for lifecycle controller.

All SQL in one place. Uses asyncpg. Returns domain models.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import asyncpg

from models import (
    Blocker,
    BlockerKind,
    Checkpoint,
    CheckpointKind,
    Gate,
    GateKind,
    GatePolicy,
    GateResolution,
    LifecycleHealth,
    LifecyclePolicy,
    LifecycleState,
    LifecycleStatus,
    Observation,
    OutboxEvent,
)


class LifecycleRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ------------------------------------------------------------------
    # Lifecycle CRUD
    # ------------------------------------------------------------------

    async def create_lifecycle(
        self,
        lifecycle_id: str,
        name: str,
        repo: str,
        repos: list[str] | None = None,
        roadmap_id: str | None = None,
        status: LifecycleStatus = LifecycleStatus.PLANNED,
        health: LifecycleHealth = LifecycleHealth.NOMINAL,
        created_by: str | None = None,
        policy: LifecyclePolicy | None = None,
    ) -> None:
        policy_json = policy.to_json() if policy else {}
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO lifecycles (id, name, repo, repos, roadmap_id, status, health, created_by, policy)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (id) DO NOTHING
                """,
                lifecycle_id,
                name,
                repo,
                json.dumps(repos) if repos else None,
                roadmap_id,
                status.value,
                health.value,
                created_by,
                json.dumps(policy_json),
            )
            # Also init lifecycle_state
            await conn.execute(
                """
                INSERT INTO lifecycle_state (lifecycle_id, status, health, policy)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (lifecycle_id) DO NOTHING
                """,
                lifecycle_id,
                status.value,
                health.value,
                json.dumps(policy_json),
            )
            # Mark dirty
            await self._mark_dirty(conn, lifecycle_id, "created")

    async def get_lifecycle_state(self, lifecycle_id: str) -> LifecycleState | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM lifecycle_state WHERE lifecycle_id = $1",
            lifecycle_id,
        )
        if not row:
            return None
        return _row_to_state(row)

    async def list_active_lifecycles(self) -> list[str]:
        rows = await self.pool.fetch(
            "SELECT lifecycle_id FROM lifecycle_state WHERE status NOT IN ('completed', 'canceled', 'archived', 'disabled')"
        )
        return [r["lifecycle_id"] for r in rows]

    # ------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------

    async def insert_observation(self, obs: Observation) -> None:
        await self.pool.execute(
            """
            INSERT INTO lifecycle_observations (lifecycle_id, source, kind, observed_at, expires_at, payload, payload_hash, confidence)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            obs.lifecycle_id,
            obs.source,
            obs.kind,
            obs.observed_at or datetime.now(timezone.utc),
            obs.expires_at or (datetime.now(timezone.utc) + __import__("datetime").timedelta(hours=24)),
            json.dumps(obs.payload),
            obs.payload_hash,
            obs.confidence,
        )
        # Mark lifecycle dirty
        async with self.pool.acquire() as conn:
            await self._mark_dirty(conn, obs.lifecycle_id, f"observation:{obs.source}:{obs.kind}")

    async def get_recent_observations(
        self, lifecycle_id: str, limit: int = 100
    ) -> list[Observation]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM lifecycle_observations
            WHERE lifecycle_id = $1 AND expires_at > now()
            ORDER BY observed_at DESC
            LIMIT $2
            """,
            lifecycle_id,
            limit,
        )
        return [_row_to_observation(r) for r in rows]

    # ------------------------------------------------------------------
    # Blockers
    # ------------------------------------------------------------------

    async def get_active_blockers(self, lifecycle_id: str) -> list[Blocker]:
        rows = await self.pool.fetch(
            "SELECT * FROM lifecycle_blockers WHERE lifecycle_id = $1 AND resolved_at IS NULL",
            lifecycle_id,
        )
        return [_row_to_blocker(r) for r in rows]

    async def upsert_blocker(self, blocker: Blocker) -> None:
        await self.pool.execute(
            """
            INSERT INTO lifecycle_blockers (id, lifecycle_id, kind, scope, blocking, summary, owner_kind, owner_id, created_at, fingerprint)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (id) DO UPDATE SET
                blocking = EXCLUDED.blocking,
                summary = EXCLUDED.summary,
                owner_kind = EXCLUDED.owner_kind,
                owner_id = EXCLUDED.owner_id,
                fingerprint = EXCLUDED.fingerprint
            """,
            blocker.id,
            blocker.lifecycle_id if hasattr(blocker, "lifecycle_id") else "",
            blocker.kind.value,
            blocker.scope,
            blocker.blocking,
            blocker.summary,
            blocker.owner_kind,
            blocker.owner_id,
            blocker.created_at or datetime.now(timezone.utc),
            "",  # fingerprint
        )

    async def resolve_blocker(self, blocker_id: str) -> None:
        await self.pool.execute(
            "UPDATE lifecycle_blockers SET resolved_at = now() WHERE id = $1",
            blocker_id,
        )

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    async def get_active_gates(self, lifecycle_id: str) -> list[Gate]:
        rows = await self.pool.fetch(
            "SELECT * FROM lifecycle_gates WHERE lifecycle_id = $1 AND resolved_at IS NULL",
            lifecycle_id,
        )
        return [_row_to_gate(r) for r in rows]

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    async def get_checkpoints(self, lifecycle_id: str) -> list[Checkpoint]:
        rows = await self.pool.fetch(
            "SELECT * FROM lifecycle_checkpoints WHERE lifecycle_id = $1",
            lifecycle_id,
        )
        return [_row_to_checkpoint(r) for r in rows]

    # ------------------------------------------------------------------
    # Reconcile queue
    # ------------------------------------------------------------------

    async def claim_next_reconcile_job(
        self, worker_id: str, lease_seconds: int = 60
    ) -> str | None:
        """Claim the next available lifecycle for reconciliation. Returns lifecycle_id or None."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT lifecycle_id FROM lifecycle_reconcile_queue
                    WHERE available_at <= now()
                      AND (
                        leased_by IS NULL
                        OR lease_expires_at IS NULL
                        OR lease_expires_at <= now()
                      )
                    ORDER BY priority DESC, available_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                )
                if not row:
                    return None
                lifecycle_id = row["lifecycle_id"]
                await conn.execute(
                    """
                    UPDATE lifecycle_reconcile_queue
                    SET leased_by = $1,
                        lease_expires_at = now() + $2::int * interval '1 second',
                        attempts = attempts + 1
                    WHERE lifecycle_id = $3
                    """,
                    worker_id,
                    lease_seconds,
                    lifecycle_id,
                )
                return lifecycle_id

    async def release_lease(self, lifecycle_id: str, requeue_delay_seconds: int = 0) -> None:
        await self.pool.execute(
            """
            UPDATE lifecycle_reconcile_queue
            SET leased_by = NULL,
                lease_expires_at = NULL,
                available_at = now() + $1::int * interval '1 second'
            WHERE lifecycle_id = $2
            """,
            requeue_delay_seconds,
            lifecycle_id,
        )

    async def delete_reconcile_job(self, lifecycle_id: str) -> None:
        await self.pool.execute(
            "DELETE FROM lifecycle_reconcile_queue WHERE lifecycle_id = $1",
            lifecycle_id,
        )

    async def enqueue_sweep(self) -> int:
        """Enqueue all active lifecycles for periodic sweep. Returns count."""
        result = await self.pool.execute(
            """
            INSERT INTO lifecycle_reconcile_queue (lifecycle_id, reason, available_at)
            SELECT id, 'periodic_sweep', now()
            FROM lifecycles
            WHERE status NOT IN ('completed', 'canceled', 'archived', 'disabled')
            ON CONFLICT (lifecycle_id)
            DO UPDATE SET reason = 'periodic_sweep', available_at = now()
            """
        )
        # asyncpg execute returns a status string like "INSERT 0 5"
        parts = result.split()
        return int(parts[-1]) if parts else 0

    # ------------------------------------------------------------------
    # State persistence (transactional)
    # ------------------------------------------------------------------

    async def persist_reconcile_result(
        self,
        lifecycle_id: str,
        state: LifecycleState,
        outbox_events: list[OutboxEvent],
    ) -> None:
        """Atomically update state, history, and outbox."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Update lifecycle_state
                await conn.execute(
                    """
                    UPDATE lifecycle_state
                    SET status = $1, health = $2, phase = $3, progress_percent = $4,
                        roadmap_version = $5, status_reason = $6, health_reason = $7,
                        last_reconciled_at = $8, state_version = $9, state_fingerprint = $10,
                        updated_at = now(), policy = $11
                    WHERE lifecycle_id = $12
                    """,
                    state.status.value,
                    state.health.value,
                    state.phase,
                    state.progress_percent,
                    state.roadmap_version,
                    state.status_reason,
                    state.health_reason,
                    state.last_reconciled_at,
                    state.state_version,
                    state.state_fingerprint,
                    json.dumps(state.policy.to_json()),
                    lifecycle_id,
                )
                # Insert history
                await conn.execute(
                    """
                    INSERT INTO lifecycle_status_history
                        (lifecycle_id, status, health, phase, progress_percent, roadmap_version, status_reason, transition, blockers, signals, policy)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    """,
                    lifecycle_id,
                    state.status.value,
                    state.health.value,
                    state.phase,
                    state.progress_percent,
                    state.roadmap_version,
                    state.status_reason,
                    json.dumps({"reason": state.status_reason, "computed": True}),
                    None,  # blockers — could populate from result
                    None,  # signals
                    json.dumps(state.policy.to_json()),
                )
                # Insert outbox events
                for event in outbox_events:
                    await conn.execute(
                        """
                        INSERT INTO lifecycle_event_outbox
                            (lifecycle_id, event_type, payload, headers, created_at)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        event.lifecycle_id,
                        event.event_type,
                        json.dumps(event.payload),
                        json.dumps(event.headers) if event.headers else None,
                        event.created_at or datetime.now(timezone.utc),
                    )

    # ------------------------------------------------------------------
    # Outbox publisher
    # ------------------------------------------------------------------

    async def get_unpublished_outbox(self, batch_size: int = 100) -> list[OutboxEvent]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM lifecycle_event_outbox
            WHERE published_at IS NULL
            ORDER BY created_at ASC
            LIMIT $1
            """,
            batch_size,
        )
        return [_row_to_outbox_event(r) for r in rows]

    async def mark_outbox_published(self, outbox_id: int) -> None:
        await self.pool.execute(
            "UPDATE lifecycle_event_outbox SET published_at = now(), publish_attempts = publish_attempts + 1 WHERE id = $1",
            outbox_id,
        )

    async def mark_outbox_failed(self, outbox_id: int, error: str) -> None:
        await self.pool.execute(
            "UPDATE lifecycle_event_outbox SET publish_attempts = publish_attempts + 1, error = $1 WHERE id = $2",
            error,
            outbox_id,
        )

    # ------------------------------------------------------------------
    # Sentinel heartbeats
    # ------------------------------------------------------------------

    async def upsert_heartbeat(
        self,
        sentinel_id: str,
        scope_kind: str,
        scope_id: str | None,
        status: str,
        error_summary: str | None = None,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO sentinel_heartbeats (sentinel_id, scope_kind, scope_id, last_seen_at, status, error_summary)
            VALUES ($1, $2, $3, now(), $4, $5)
            ON CONFLICT (sentinel_id) DO UPDATE SET
                scope_kind = EXCLUDED.scope_kind,
                scope_id = EXCLUDED.scope_id,
                last_seen_at = EXCLUDED.last_seen_at,
                status = EXCLUDED.status,
                error_summary = EXCLUDED.error_summary,
                updated_at = now()
            """,
            sentinel_id,
            scope_kind,
            scope_id,
            status,
            error_summary,
        )

    async def get_sentinel_health(self) -> dict[str, str]:
        """Return map of sentinel_id -> status."""
        rows = await self.pool.fetch("SELECT sentinel_id, status FROM sentinel_heartbeats")
        return {r["sentinel_id"]: r["status"] for r in rows}

    async def get_stale_sentinels(self, threshold_minutes: int = 15) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT sentinel_id, scope_kind, scope_id, last_seen_at, status
            FROM sentinel_heartbeats
            WHERE last_seen_at < now() - $1::int * interval '1 minute'
            """,
            threshold_minutes,
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _mark_dirty(
        self, conn: asyncpg.Connection, lifecycle_id: str, reason: str
    ) -> None:
        await conn.execute(
            """
            INSERT INTO lifecycle_reconcile_queue (lifecycle_id, reason, available_at)
            VALUES ($1, $2, now())
            ON CONFLICT (lifecycle_id)
            DO UPDATE SET reason = EXCLUDED.reason, available_at = now()
            """,
            lifecycle_id,
            reason,
        )


# ------------------------------------------------------------------
# Row mappers
# ------------------------------------------------------------------


def _row_to_state(row: asyncpg.Record) -> LifecycleState:
    policy = _json_object(row["policy"])
    return LifecycleState(
        lifecycle_id=row["lifecycle_id"],
        status=LifecycleStatus(row["status"]),
        health=LifecycleHealth(row["health"]),
        phase=row["phase"],
        progress_percent=row["progress_percent"] or 0.0,
        roadmap_version=row["roadmap_version"] or 1,
        status_reason=row["status_reason"] or "",
        health_reason=row["health_reason"] or "",
        last_progress_at=row["last_progress_at"],
        last_reconciled_at=row["last_reconciled_at"],
        state_version=row["state_version"] or 1,
        state_fingerprint=row["state_fingerprint"] or "",
        policy=LifecyclePolicy.from_json(policy),
    )


def _json_object(value: object) -> dict:
    if not value:
        return {}
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    if isinstance(value, dict):
        return value
    return dict(value)


def _row_to_observation(row: asyncpg.Record) -> Observation:
    return Observation(
        id=row["id"],
        lifecycle_id=row["lifecycle_id"],
        source=row["source"],
        kind=row["kind"],
        observed_at=row["observed_at"],
        expires_at=row["expires_at"],
        payload=json.loads(row["payload"]) if row["payload"] else {},
        confidence=row["confidence"] or 1.0,
    )


def _row_to_blocker(row: asyncpg.Record) -> Blocker:
    return Blocker(
        id=row["id"],
        kind=BlockerKind(row["kind"]),
        scope=row["scope"],
        blocking=row["blocking"],
        summary=row["summary"] or "",
        owner_kind=row["owner_kind"],
        owner_id=row["owner_id"],
        created_at=row["created_at"],
    )


def _row_to_gate(row: asyncpg.Record) -> Gate:
    return Gate(
        id=row["id"],
        kind=GateKind(row["kind"]),
        blocking=row["blocking"],
        reason=row["reason"] or "",
        continue_policy=GatePolicy(row["continue_policy"]),
        owner_kind=row["owner_kind"],
        owner_id=row["owner_id"],
        sla_due_at=row["sla_due_at"],
        triggered_by_checkpoint_id=row["triggered_by_checkpoint_id"],
        opened_at=row["opened_at"],
        resolved_at=row["resolved_at"],
        resolution=GateResolution(row["resolution"]) if row["resolution"] else None,
    )


def _row_to_checkpoint(row: asyncpg.Record) -> Checkpoint:
    return Checkpoint(
        id=row["id"],
        kind=CheckpointKind(row["kind"]),
        name=row["name"],
        roadmap_version=row["roadmap_version"] or 1,
        phase_id=row["phase_id"],
        reached_at=row["reached_at"],
        invalidated_at=row["invalidated_at"],
        evidence=json.loads(row["evidence"]) if row["evidence"] else [],
    )


def _row_to_outbox_event(row: asyncpg.Record) -> OutboxEvent:
    return OutboxEvent(
        id=row["id"],
        lifecycle_id=row["lifecycle_id"],
        event_type=row["event_type"],
        payload=json.loads(row["payload"]) if row["payload"] else {},
        headers=json.loads(row["headers"]) if row["headers"] else {},
        created_at=row["created_at"],
        published_at=row["published_at"],
        publish_attempts=row["publish_attempts"] or 0,
        error=row["error"],
    )
