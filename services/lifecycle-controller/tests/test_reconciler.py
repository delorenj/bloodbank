"""Tests for the lifecycle reconciler, dogfooded on Drumjangler."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


from models import (
    Blocker,
    BlockerKind,
    Checkpoint,
    CheckpointKind,
    Gate,
    GateKind,
    LifecycleHealth,
    LifecyclePolicy,
    LifecycleState,
    LifecycleStatus,
    Observation,
)
from reconciler import evaluate_lifecycle, reconcile


NOW = datetime.now(timezone.utc)


def _observation(kind: str, payload: dict, source: str = "test-sentinel") -> Observation:
    return Observation(
        lifecycle_id="lc_drumjangler_mvp",
        source=source,
        kind=kind,
        observed_at=NOW,
        payload=payload,
    )


def _state(
    status: LifecycleStatus = LifecycleStatus.ACTIVE,
    health: LifecycleHealth = LifecycleHealth.NOMINAL,
    last_progress_at: datetime | None = None,
    policy: LifecyclePolicy | None = None,
) -> LifecycleState:
    return LifecycleState(
        lifecycle_id="lc_drumjangler_mvp",
        status=status,
        health=health,
        last_progress_at=last_progress_at or NOW,
        policy=policy or LifecyclePolicy(),
    )


class TestEvaluateLifecycle:
    """Unit tests for the core evaluation logic."""

    def test_terminal_states_return_nominal(self):
        for status in [
            LifecycleStatus.PAUSED,
            LifecycleStatus.DISABLED,
            LifecycleStatus.COMPLETED,
            LifecycleStatus.CANCELED,
            LifecycleStatus.ARCHIVED,
        ]:
            state = _state(status=status)
            verdict = evaluate_lifecycle(state, [], [], [], [], {})
            assert verdict.health == LifecycleHealth.NOMINAL
            assert verdict.reason == "INTENTIONAL_NON_PROGRESS"

    def test_no_runnable_work_with_blockers_is_blocked(self):
        state = _state()
        obs = [_observation("work_items_snapshot", {"open_count": 5, "runnable_count": 0})]
        blockers = [Blocker(id="blk_1", kind=BlockerKind.MISSING_HUMAN_INPUT)]
        verdict = evaluate_lifecycle(state, obs, blockers, [], [], {})
        assert verdict.status == LifecycleStatus.BLOCKED
        assert verdict.health == LifecycleHealth.BLOCKED
        assert verdict.reason == "NO_RUNNABLE_WORK"

    def test_runnable_work_but_no_progress_is_stalled(self):
        state = _state(last_progress_at=NOW - timedelta(minutes=120))
        obs = [_observation("work_items_snapshot", {"open_count": 5, "runnable_count": 3})]
        verdict = evaluate_lifecycle(state, obs, [], [], [], {})
        assert verdict.status == LifecycleStatus.ACTIVE
        assert verdict.health == LifecycleHealth.STALLED
        assert verdict.reason == "RUNNABLE_WORK_NOT_ADVANCING"

    def test_repo_activity_observation_can_drive_stalled_verdict(self):
        state = _state(last_progress_at=None)
        obs = [
            _observation("work_items_snapshot", {"open_count": 5, "runnable_count": 3}),
            _observation(
                "repo_activity_snapshot",
                {"last_commit_at": (NOW - timedelta(minutes=120)).isoformat()},
            ),
        ]
        verdict = evaluate_lifecycle(state, obs, [], [], [], {})
        assert verdict.status == LifecycleStatus.ACTIVE
        assert verdict.health == LifecycleHealth.STALLED
        assert verdict.reason == "RUNNABLE_WORK_NOT_ADVANCING"

    def test_blocking_gate_is_waiting(self):
        state = _state()
        gate = Gate(id="gate_1", kind=GateKind.HUMAN_REVIEW, blocking=True, reason="MVP review")
        verdict = evaluate_lifecycle(state, [], [], [gate], [], {})
        assert verdict.status == LifecycleStatus.WAITING
        assert verdict.health == LifecycleHealth.NOMINAL
        assert verdict.reason == "BLOCKING_GATE_OPEN"

    def test_sla_breached_gate_is_at_risk(self):
        state = _state()
        gate = Gate(
            id="gate_1",
            kind=GateKind.HUMAN_REVIEW,
            blocking=True,
            sla_due_at=NOW - timedelta(hours=1),
        )
        verdict = evaluate_lifecycle(state, [], [], [gate], [], {})
        assert verdict.status == LifecycleStatus.WAITING
        assert verdict.health == LifecycleHealth.AT_RISK

    def test_degraded_observer_degrades_health(self):
        state = _state()
        obs = [_observation("work_items_snapshot", {"open_count": 3, "runnable_count": 2})]
        sentinel_health = {"plane-sentinel": "missing"}
        verdict = evaluate_lifecycle(state, obs, [], [], [], sentinel_health)
        assert verdict.status == LifecycleStatus.ACTIVE
        assert verdict.health == LifecycleHealth.DEGRADED
        assert verdict.reason == "OBSERVABILITY_DEGRADED"

    def test_progressing_is_default(self):
        state = _state()
        obs = [_observation("work_items_snapshot", {"open_count": 3, "runnable_count": 2})]
        verdict = evaluate_lifecycle(state, obs, [], [], [], {})
        assert verdict.status == LifecycleStatus.ACTIVE
        assert verdict.health == LifecycleHealth.NOMINAL
        assert verdict.reason == "PROGRESSING"

    def test_latest_observation_per_kind_wins(self):
        state = _state()
        stale = _observation(
            "work_items_snapshot",
            {"open_count": 8, "runnable_count": 5},
            source="plane-sentinel",
        )
        stale.observed_at = NOW - timedelta(minutes=10)
        latest = _observation(
            "work_items_snapshot",
            {"open_count": 5, "runnable_count": 0},
            source="plane-sentinel",
        )
        latest.observed_at = NOW
        blocker = Blocker(id="blk_1", kind=BlockerKind.MISSING_HUMAN_INPUT)

        verdict = evaluate_lifecycle(state, [latest, stale], [blocker], [], [], {})

        assert verdict.status == LifecycleStatus.BLOCKED
        assert verdict.health == LifecycleHealth.BLOCKED
        assert verdict.signals.open_work_items == 5
        assert verdict.signals.runnable_work_items == 0


class TestReconcileDrumjangler:
    """Dogfood: simulate Drumjangler lifecycle scenarios."""

    def test_drumjangler_mvp_creation(self):
        """Fresh lifecycle starts as planned, but reconcile evaluates to active (no blockers)."""
        result = reconcile(
            lifecycle_id="lc_drumjangler_mvp",
            previous_state=None,
            observations=[],
            active_blockers=[],
            active_gates=[],
            checkpoints=[],
            sentinel_health={},
        )
        # With no previous state and no observations, default policy says active/progressing
        assert result.current_state.status == LifecycleStatus.ACTIVE
        assert result.current_state.health == LifecycleHealth.NOMINAL

    def test_drumjangler_active_with_tickets(self):
        """Drumjangler has open tickets, agents working."""
        prev = _state(status=LifecycleStatus.ACTIVE, health=LifecycleHealth.NOMINAL)
        obs = [
            _observation("work_items_snapshot", {"open_count": 8, "runnable_count": 5}),
            _observation("agent_runs_snapshot", {"active_count": 2}),
        ]
        result = reconcile(
            lifecycle_id="lc_drumjangler_mvp",
            previous_state=prev,
            observations=obs,
            active_blockers=[],
            active_gates=[],
            checkpoints=[],
            sentinel_health={},
        )
        assert result.current_state.status == LifecycleStatus.ACTIVE
        assert result.current_state.health == LifecycleHealth.NOMINAL
        # state_changed is True because last_reconciled_at updated and state_version incremented
        assert result.state_changed is True

    def test_drumjangler_blocked_all_tickets_waiting(self):
        """All Drumjangler tickets waiting on human input — blocked."""
        prev = _state(status=LifecycleStatus.ACTIVE, health=LifecycleHealth.NOMINAL)
        obs = [_observation("work_items_snapshot", {"open_count": 5, "runnable_count": 0})]
        blockers = [Blocker(id="blk_1", kind=BlockerKind.MISSING_HUMAN_INPUT)]
        result = reconcile(
            lifecycle_id="lc_drumjangler_mvp",
            previous_state=prev,
            observations=obs,
            active_blockers=blockers,
            active_gates=[],
            checkpoints=[],
            sentinel_health={},
        )
        assert result.status_changed is True
        assert result.current_state.status == LifecycleStatus.BLOCKED
        assert result.current_state.health == LifecycleHealth.BLOCKED
        # Should emit lifecycle.status.updated
        assert any(e.event_type == "bloodbank.v1.lifecycle.status.updated" for e in result.outbox_events)

    def test_drumjangler_stalled_no_commits(self):
        """No commits in 2+ hours despite runnable work."""
        prev = _state(
            status=LifecycleStatus.ACTIVE,
            health=LifecycleHealth.NOMINAL,
            last_progress_at=NOW - timedelta(minutes=120),
        )
        obs = [
            _observation("work_items_snapshot", {"open_count": 3, "runnable_count": 2}),
            _observation("repo_activity_snapshot", {"last_commit_at": (NOW - timedelta(minutes=120)).isoformat()}),
        ]
        result = reconcile(
            lifecycle_id="lc_drumjangler_mvp",
            previous_state=prev,
            observations=obs,
            active_blockers=[],
            active_gates=[],
            checkpoints=[],
            sentinel_health={},
        )
        assert result.current_state.health == LifecycleHealth.STALLED
        assert result.health_changed is True

    def test_drumjangler_mvp_checkpoint_reached(self):
        """MVP checkpoint reached, human review gate opens."""
        prev = _state(status=LifecycleStatus.ACTIVE, health=LifecycleHealth.NOMINAL)
        checkpoint = Checkpoint(id="chk_mvp", kind=CheckpointKind.MVP, name="MVP 1", reached_at=NOW)
        gate = Gate(id="gate_mvp", kind=GateKind.HUMAN_REVIEW, blocking=True, reason="MVP reached")
        result = reconcile(
            lifecycle_id="lc_drumjangler_mvp",
            previous_state=prev,
            observations=[],
            active_blockers=[],
            active_gates=[gate],
            checkpoints=[checkpoint],
            sentinel_health={},
        )
        assert result.current_state.status == LifecycleStatus.WAITING
        assert result.current_state.health == LifecycleHealth.NOMINAL
