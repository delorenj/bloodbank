#!/usr/bin/env python3
"""Dogfood script: create a Drumjangler lifecycle and pump observations through it.

This script exercises the full lifecycle-controller stack:
1. Creates a lifecycle for Drumjangler MVP
2. Inserts observations (as if from sentinels)
3. Marks lifecycle dirty (triggers reconcile)
4. Queries state to show the reconciler computed the right status

Usage:
    cd services/lifecycle-controller
    PYTHONPATH=src python3 scripts/dogfood_drumjangler.py

Requires: docker compose postgres running with lifecycle tables
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# Add src to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from models import (
    Blocker,
    BlockerKind,
    Gate,
    GateKind,
    LifecycleHealth,
    LifecyclePolicy,
    LifecycleState,
    LifecycleStatus,
    Observation,
)
from reconciler import reconcile

LIFECYCLE_ID = "lc_drumjangler_mvp"
REPO = "drumjangler"


def psql(sql: str) -> list[dict]:
    """Run a SELECT via docker exec and return rows as dictionaries.

    psql's unaligned/tabular formats are fragile because ``-t`` removes column
    headers. Wrap the caller's SELECT with ``row_to_json`` so the script can
    reliably map columns by name while staying stdlib-only.
    """
    wrapped_sql = f"""
    SELECT COALESCE(json_agg(row_to_json(_dogfood_rows)), '[]'::json)
    FROM ({sql.rstrip().rstrip(';')}) AS _dogfood_rows;
    """
    cmd = [
        "docker", "exec", "-i", "bloodbank-postgres",
        "psql", "-U", "candystore", "-d", "candystore",
        "-t", "-A",
        "-c", wrapped_sql,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"SQL error: {result.stderr}")
        return []
    raw = result.stdout.strip()
    if not raw:
        return []
    return json.loads(raw)


def psql_exec(sql: str) -> str:
    """Run SQL via docker exec, return raw output."""
    cmd = [
        "docker", "exec", "-i", "bloodbank-postgres",
        "psql", "-U", "candystore", "-d", "candystore",
        "-c", sql,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout + result.stderr


def reset_dogfood_data() -> None:
    """Reset prior Drumjangler demo rows so every run is repeatable."""
    psql_exec(f"DELETE FROM lifecycles WHERE id = '{LIFECYCLE_ID}';")
    psql_exec("DELETE FROM sentinel_heartbeats WHERE sentinel_id LIKE 'dogfood-%';")
    print(f"🧹 Reset prior dogfood rows for {LIFECYCLE_ID}")


def seed_lifecycle() -> None:
    """Create the Drumjangler lifecycle if it doesn't exist."""
    policy = LifecyclePolicy(
        progress_expected=True,
        stalled_after_minutes=90,
        blocked_after_minutes=15,
        observer_stale_after_minutes=10,
        sentinel_missing_after_minutes=15,
        reconcile_interval_minutes=3,
    )
    sql = f"""
    INSERT INTO lifecycles (id, name, repo, status, health, created_by, policy)
    VALUES ('{LIFECYCLE_ID}', 'Drumjangler MVP', '{REPO}', 'planned', 'nominal', 'dogfood-script', '{json.dumps(policy.to_json())}')
    ON CONFLICT (id) DO NOTHING;
    INSERT INTO lifecycle_state (lifecycle_id, status, health, policy)
    VALUES ('{LIFECYCLE_ID}', 'planned', 'nominal', '{json.dumps(policy.to_json())}')
    ON CONFLICT (lifecycle_id) DO NOTHING;
    INSERT INTO lifecycle_reconcile_queue (lifecycle_id, reason, available_at)
    VALUES ('{LIFECYCLE_ID}', 'created', now())
    ON CONFLICT (lifecycle_id) DO NOTHING;
    """
    psql_exec(sql)
    print(f"✅ Lifecycle {LIFECYCLE_ID} created (or already exists)")


def insert_observations(observations: list[Observation]) -> None:
    """Insert observations and mark lifecycle dirty."""
    for obs in observations:
        payload_json = json.dumps(obs.payload).replace("'", "''")
        sql = f"""
        INSERT INTO lifecycle_observations (lifecycle_id, source, kind, observed_at, payload)
        VALUES ('{obs.lifecycle_id}', '{obs.source}', '{obs.kind}', now(), '{payload_json}');
        """
        psql_exec(sql)
    # Mark dirty
    psql_exec(f"""
        INSERT INTO lifecycle_reconcile_queue (lifecycle_id, reason, available_at)
        VALUES ('{LIFECYCLE_ID}', 'observation', now())
        ON CONFLICT (lifecycle_id) DO UPDATE SET reason = 'observation', available_at = now();
    """)
    print(f"   Inserted {len(observations)} observations")


def insert_blocker(blocker: Blocker) -> None:
    """Insert a blocker."""
    sql = f"""
    INSERT INTO lifecycle_blockers (id, lifecycle_id, kind, scope, blocking, summary)
    VALUES ('{blocker.id}', '{blocker.lifecycle_id}', '{blocker.kind.value}', '{blocker.scope}', {str(blocker.blocking).lower()}, '{blocker.summary}')
    ON CONFLICT (id) DO UPDATE SET blocking = EXCLUDED.blocking, summary = EXCLUDED.summary;
    """
    psql_exec(sql)
    print(f"   Inserted blocker: {blocker.kind.value}")


def resolve_blocker(blocker_id: str) -> None:
    """Resolve a blocker."""
    psql_exec(f"UPDATE lifecycle_blockers SET resolved_at = now() WHERE id = '{blocker_id}';")
    print(f"   Resolved blocker: {blocker_id}")


def insert_gate(gate: Gate) -> None:
    """Insert a gate."""
    sql = f"""
    INSERT INTO lifecycle_gates (id, lifecycle_id, kind, blocking, reason, owner_kind, owner_id, opened_at)
    VALUES ('{gate.id}', '{LIFECYCLE_ID}', '{gate.kind.value}', {str(gate.blocking).lower()}, '{gate.reason}', '{gate.owner_kind or ''}', '{gate.owner_id or ''}', now())
    ON CONFLICT (id) DO UPDATE SET blocking = EXCLUDED.blocking, reason = EXCLUDED.reason, resolved_at = NULL;
    """
    psql_exec(sql)
    print(f"   Inserted gate: {gate.kind.value}")


def _parse_dt(value: object) -> datetime | None:
    """Parse Postgres JSON timestamp values back into datetimes."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def get_lifecycle_state() -> LifecycleState | None:
    """Fetch current state from DB."""
    rows = psql(f"SELECT * FROM lifecycle_state WHERE lifecycle_id = '{LIFECYCLE_ID}'")
    if not rows:
        return None
    row = rows[0]
    return LifecycleState(
        lifecycle_id=row["lifecycle_id"],
        status=LifecycleStatus(row["status"]),
        health=LifecycleHealth(row["health"]),
        phase=row.get("phase") or None,
        progress_percent=float(row.get("progress_percent", 0) or 0),
        status_reason=row.get("status_reason") or "",
    )


def get_observations() -> list[Observation]:
    """Fetch recent observations."""
    rows = psql(f"SELECT * FROM lifecycle_observations WHERE lifecycle_id = '{LIFECYCLE_ID}' AND expires_at > now() ORDER BY observed_at DESC LIMIT 100")
    return [
        Observation(
            lifecycle_id=r["lifecycle_id"],
            source=r["source"],
            kind=r["kind"],
            observed_at=_parse_dt(r.get("observed_at")),
            payload=r["payload"] if isinstance(r.get("payload"), dict) else json.loads(r["payload"]),
        )
        for r in rows
    ]


def get_blockers() -> list[Blocker]:
    """Fetch active blockers."""
    rows = psql(f"SELECT * FROM lifecycle_blockers WHERE lifecycle_id = '{LIFECYCLE_ID}' AND resolved_at IS NULL")
    return [
        Blocker(
            id=r["id"],
            lifecycle_id=r["lifecycle_id"],
            kind=BlockerKind(r["kind"]),
            scope=r["scope"],
            blocking=bool(r["blocking"]),
            summary=r.get("summary") or "",
        )
        for r in rows
    ]


def get_gates() -> list[Gate]:
    """Fetch active gates."""
    rows = psql(f"SELECT * FROM lifecycle_gates WHERE lifecycle_id = '{LIFECYCLE_ID}' AND resolved_at IS NULL")
    return [
        Gate(
            id=r["id"],
            kind=GateKind(r["kind"]),
            blocking=bool(r["blocking"]),
            reason=r.get("reason") or "",
            owner_kind=r.get("owner_kind") or None,
            owner_id=r.get("owner_id") or None,
        )
        for r in rows
    ]


def get_checkpoints() -> list:
    """Fetch checkpoints."""
    rows = psql(f"SELECT * FROM lifecycle_checkpoints WHERE lifecycle_id = '{LIFECYCLE_ID}'")
    return rows


def get_sentinel_health() -> dict[str, str]:
    """Fetch sentinel health."""
    rows = psql("SELECT sentinel_id, status FROM sentinel_heartbeats")
    return {r["sentinel_id"]: r["status"] for r in rows}


def run_reconcile_and_show() -> None:
    """Manually trigger reconcile and show results."""
    print("\n🔧 Running reconcile...")

    state = get_lifecycle_state()
    observations = get_observations()
    blockers = get_blockers()
    gates = get_gates()
    checkpoints = get_checkpoints()
    sentinel_health = get_sentinel_health()

    if not state:
        print("   ❌ No state found")
        return

    result = reconcile(
        lifecycle_id=LIFECYCLE_ID,
        previous_state=state,
        observations=observations,
        active_blockers=blockers,
        active_gates=gates,
        checkpoints=checkpoints,
        sentinel_health=sentinel_health,
    )

    print("\n📊 Reconcile Result:")
    print(f"   Previous: {result.previous_state.status.value if result.previous_state else 'N/A'} / {result.previous_state.health.value if result.previous_state else 'N/A'}")
    print(f"   Current:  {result.current_state.status.value} / {result.current_state.health.value}")
    print(f"   Reason:   {result.current_state.status_reason}")
    print(f"   Changed:  status={result.status_changed}, health={result.health_changed}")
    print(f"   Outbox:   {len(result.outbox_events)} events")
    for evt in result.outbox_events:
        print(f"      - {evt.event_type}")

    # Persist to DB
    psql_exec(f"""
        UPDATE lifecycle_state
        SET status = '{result.current_state.status.value}',
            health = '{result.current_state.health.value}',
            status_reason = '{result.current_state.status_reason}',
            last_reconciled_at = now(),
            state_version = state_version + 1
        WHERE lifecycle_id = '{LIFECYCLE_ID}';
    """)
    for evt in result.outbox_events:
        payload_json = json.dumps(evt.payload).replace("'", "''")
        psql_exec(f"""
            INSERT INTO lifecycle_event_outbox (lifecycle_id, event_type, payload, created_at)
            VALUES ('{LIFECYCLE_ID}', '{evt.event_type}', '{payload_json}', now());
        """)
    print("   ✅ Persisted to DB")


def show_current_state() -> None:
    """Print current lifecycle state."""
    state = get_lifecycle_state()
    if not state:
        print("   ❌ No state found")
        return

    print(f"\n📈 Current State for {LIFECYCLE_ID}:")
    print(f"   Status:     {state.status.value}")
    print(f"   Health:     {state.health.value}")
    print(f"   Phase:      {state.phase or 'N/A'}")
    print(f"   Progress:   {state.progress_percent}%")
    print(f"   Reason:     {state.status_reason}")


def show_outbox() -> None:
    """Print unpublished outbox events."""
    rows = psql(f"SELECT * FROM lifecycle_event_outbox WHERE lifecycle_id = '{LIFECYCLE_ID}' AND published_at IS NULL LIMIT 10")
    if not rows:
        print("\n📭 Outbox: empty")
        return

    print(f"\n📬 Outbox ({len(rows)} unpublished):")
    for r in rows:
        print(f"   [{r.get('id', '?')}] {r['event_type']}")


def scenario_1_active_with_tickets() -> None:
    """Drumjangler has open tickets, agents working — should be ACTIVE/NOMINAL."""
    print("\n📋 Scenario 1: Active with tickets")
    observations = [
        Observation(
            lifecycle_id=LIFECYCLE_ID,
            source="plane-sentinel",
            kind="work_items_snapshot",
            payload={"open_count": 8, "runnable_count": 5, "completed_count": 12},
        ),
        Observation(
            lifecycle_id=LIFECYCLE_ID,
            source="agent-sentinel",
            kind="agent_runs_snapshot",
            payload={"active_count": 2, "failed_count": 0},
        ),
        Observation(
            lifecycle_id=LIFECYCLE_ID,
            source="git-sentinel",
            kind="repo_activity_snapshot",
            payload={
                "last_commit_at": datetime.now(timezone.utc).isoformat(),
                "open_prs": 3,
            },
        ),
    ]
    insert_observations(observations)


def scenario_2_blocked_all_waiting() -> None:
    """All tickets waiting on human — should become BLOCKED."""
    print("\n📋 Scenario 2: All tickets waiting on human input")
    observations = [
        Observation(
            lifecycle_id=LIFECYCLE_ID,
            source="plane-sentinel",
            kind="work_items_snapshot",
            payload={"open_count": 5, "runnable_count": 0, "waiting_on_human": 5},
        ),
    ]
    insert_observations(observations)
    insert_blocker(
        Blocker(
            id="blk_human_1",
            lifecycle_id=LIFECYCLE_ID,
            kind=BlockerKind.MISSING_HUMAN_INPUT,
            summary="All remaining tickets require human clarification",
        )
    )


def scenario_3_stalled_no_commits() -> None:
    """No commits in 2+ hours despite runnable work — should become STALLED."""
    print("\n📋 Scenario 3: Stalled — no commits in 2 hours")
    observations = [
        Observation(
            lifecycle_id=LIFECYCLE_ID,
            source="plane-sentinel",
            kind="work_items_snapshot",
            payload={"open_count": 3, "runnable_count": 2},
        ),
        Observation(
            lifecycle_id=LIFECYCLE_ID,
            source="git-sentinel",
            kind="repo_activity_snapshot",
            payload={
                "last_commit_at": (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat(),
                "open_prs": 1,
            },
        ),
    ]
    insert_observations(observations)


def scenario_4_mvp_gate() -> None:
    """MVP checkpoint reached, human review gate opens — should become WAITING."""
    print("\n📋 Scenario 4: MVP checkpoint reached, human review gate")
    resolve_blocker("blk_human_1")
    insert_gate(
        Gate(
            id="gate_mvp_review",
            kind=GateKind.HUMAN_REVIEW,
            blocking=True,
            reason="MVP checkpoint reached — requires human sign-off before release",
            owner_kind="human",
            owner_id="jarad",
        )
    )


def main() -> None:
    print("=" * 60)
    print("🥁 Drumjangler Lifecycle Controller Dogfood")
    print("=" * 60)

    # Setup
    reset_dogfood_data()
    seed_lifecycle()

    # Scenario 1: Active with tickets
    scenario_1_active_with_tickets()
    run_reconcile_and_show()
    show_current_state()

    # Scenario 2: Blocked — all waiting
    scenario_2_blocked_all_waiting()
    run_reconcile_and_show()
    show_current_state()

    # Scenario 3: Stalled — no commits
    scenario_3_stalled_no_commits()
    run_reconcile_and_show()
    show_current_state()

    # Scenario 4: MVP gate
    scenario_4_mvp_gate()
    run_reconcile_and_show()
    show_current_state()

    # Show outbox
    show_outbox()

    print("\n" + "=" * 60)
    print("✅ Dogfood complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
