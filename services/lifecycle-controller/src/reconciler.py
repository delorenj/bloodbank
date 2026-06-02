"""Lifecycle reconciliation engine.

The reconciler is the single authority for lifecycle state. It:
1. Loads the current lifecycle state + policy
2. Collects latest observations from all sentinels
3. Evaluates the deterministic `evaluate_lifecycle()` function
4. Computes deltas (status changed? health changed? blockers changed?)
5. Returns a ReconcileResult with state updates and outbox events

No side effects. The caller (worker) persists to DB and publishes events.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from models import (
    Blocker,
    BlockerKind,
    Checkpoint,
    Gate,
    GateKind,
    LifecycleHealth,
    LifecycleSignals,
    LifecycleState,
    LifecycleStatus,
    LifecycleVerdict,
    Observation,
    OutboxEvent,
)


@dataclass
class ReconcileResult:
    lifecycle_id: str
    previous_state: LifecycleState | None
    current_state: LifecycleState
    state_changed: bool = False
    status_changed: bool = False
    health_changed: bool = False
    blockers_delta: list[dict] = field(default_factory=list)  # {action: detected|resolved|updated, blocker: Blocker}
    checkpoints_delta: list[dict] = field(default_factory=list)
    gates_delta: list[dict] = field(default_factory=list)
    outbox_events: list[OutboxEvent] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(state: LifecycleState) -> str:
    """Deterministic hash of state for quick diff."""
    payload = {
        "status": state.status.value,
        "health": state.health.value,
        "phase": state.phase,
        "progress_percent": state.progress_percent,
        "roadmap_version": state.roadmap_version,
        "status_reason": state.status_reason,
        "health_reason": state.health_reason,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def evaluate_lifecycle(
    current_state: LifecycleState,
    observations: list[Observation],
    active_blockers: list[Blocker],
    active_gates: list[Gate],
    checkpoints: list[Checkpoint],
    sentinel_health: dict[str, str],  # sentinel_id -> status
) -> LifecycleVerdict:
    """Compute the authoritative lifecycle verdict from all inputs.

    This is the core policy function. It must be deterministic and
    side-effect-free so multiple workers can run it without divergence.
    """
    policy = current_state.policy

    # --- Terminal states: no evaluation needed --------------------------------
    if current_state.status in (
        LifecycleStatus.PAUSED,
        LifecycleStatus.DISABLED,
        LifecycleStatus.COMPLETED,
        LifecycleStatus.CANCELED,
        LifecycleStatus.ARCHIVED,
    ):
        return LifecycleVerdict(
            status=current_state.status,
            health=LifecycleHealth.NOMINAL,
            reason="INTENTIONAL_NON_PROGRESS",
            signals=LifecycleSignals(),
        )

    # --- Aggregate signals from observations ----------------------------------
    signals = _aggregate_signals(observations)
    signals.open_blockers = len([b for b in active_blockers if b.blocking])

    # --- Check for blocking gates --------------------------------------------
    blocking_gates = [g for g in active_gates if g.blocking and g.resolved_at is None]
    if blocking_gates:
        sla_breached = any(
            g.sla_due_at is not None and g.sla_due_at < _now()
            for g in blocking_gates
        )
        return LifecycleVerdict(
            status=LifecycleStatus.WAITING,
            health=LifecycleHealth.AT_RISK if sla_breached else LifecycleHealth.NOMINAL,
            reason="BLOCKING_GATE_OPEN",
            blockers=[
                Blocker(
                    id=g.id,
                    kind=BlockerKind.HUMAN_REVIEW_REQUIRED if g.kind == GateKind.HUMAN_REVIEW else BlockerKind.DEPENDENCY_NOT_READY,
                    scope="lifecycle",
                    blocking=True,
                    summary=g.reason or f"Gate {g.kind.value} open",
                    owner_kind=g.owner_kind,
                    owner_id=g.owner_id,
                )
                for g in blocking_gates
            ],
            signals=signals,
        )

    # --- Check for no runnable work ------------------------------------------
    if signals.runnable_work_items == 0 and signals.open_blockers > 0:
        return LifecycleVerdict(
            status=LifecycleStatus.BLOCKED,
            health=LifecycleHealth.BLOCKED,
            reason="NO_RUNNABLE_WORK",
            blockers=active_blockers,
            signals=signals,
        )

    # --- Check for stalled progress ------------------------------------------
    observed_progress_at = signals.last_progress_at or current_state.last_progress_at
    if (
        signals.runnable_work_items > 0
        and observed_progress_at is not None
        and policy.progress_expected
    ):
        elapsed_minutes = (_now() - observed_progress_at).total_seconds() / 60
        if elapsed_minutes > policy.stalled_after_minutes:
            return LifecycleVerdict(
                status=LifecycleStatus.ACTIVE,
                health=LifecycleHealth.STALLED,
                reason="RUNNABLE_WORK_NOT_ADVANCING",
                signals=signals,
            )

    # --- Check observer health -----------------------------------------------
    degraded_observers = [s for s, status in sentinel_health.items() if status != "healthy"]
    if degraded_observers:
        return LifecycleVerdict(
            status=LifecycleStatus.ACTIVE,
            health=LifecycleHealth.DEGRADED,
            reason="OBSERVABILITY_DEGRADED",
            signals=signals,
        )

    # --- Default: progressing --------------------------------------------------
    return LifecycleVerdict(
        status=LifecycleStatus.ACTIVE,
        health=LifecycleHealth.NOMINAL,
        reason="PROGRESSING",
        signals=signals,
    )


def _aggregate_signals(observations: list[Observation]) -> LifecycleSignals:
    """Extract aggregate signals from raw observations."""
    signals = LifecycleSignals()

    for obs in sorted(observations, key=lambda item: item.observed_at or datetime.min.replace(tzinfo=timezone.utc)):
        payload = obs.payload
        if obs.kind == "work_items_snapshot":
            signals.open_work_items = payload.get("open_count", signals.open_work_items)
            signals.runnable_work_items = payload.get("runnable_count", signals.runnable_work_items)
        elif obs.kind == "agent_runs_snapshot":
            signals.active_agent_runs = payload.get("active_count", signals.active_agent_runs)
        elif obs.kind == "repo_activity_snapshot":
            if "last_commit_at" in payload:
                # Parse ISO timestamp
                try:
                    last_commit = datetime.fromisoformat(payload["last_commit_at"].replace("Z", "+00:00"))
                    signals.last_progress_at = last_commit
                except (ValueError, AttributeError):
                    pass
        elif obs.kind == "ci_status_snapshot":
            # CI failures contribute to blockers, not signals directly
            pass

    return signals


def reconcile(
    lifecycle_id: str,
    previous_state: LifecycleState | None,
    observations: list[Observation],
    active_blockers: list[Blocker],
    active_gates: list[Gate],
    checkpoints: list[Checkpoint],
    sentinel_health: dict[str, str],
) -> ReconcileResult:
    """Run the full reconciliation cycle.

    Returns a ReconcileResult with state changes and outbox events.
    The caller is responsible for persisting state and publishing events.
    """
    # Start from previous state or default
    if previous_state is None:
        current_state = LifecycleState(
            lifecycle_id=lifecycle_id,
            status=LifecycleStatus.PLANNED,
            health=LifecycleHealth.NOMINAL,
        )
    else:
        current_state = LifecycleState(
            lifecycle_id=previous_state.lifecycle_id,
            status=previous_state.status,
            health=previous_state.health,
            phase=previous_state.phase,
            progress_percent=previous_state.progress_percent,
            roadmap_version=previous_state.roadmap_version,
            status_reason=previous_state.status_reason,
            health_reason=previous_state.health_reason,
            last_progress_at=previous_state.last_progress_at,
            last_reconciled_at=_now(),
            state_version=previous_state.state_version + 1,
            policy=previous_state.policy,
        )

    # Compute verdict
    verdict = evaluate_lifecycle(
        current_state=current_state,
        observations=observations,
        active_blockers=active_blockers,
        active_gates=active_gates,
        checkpoints=checkpoints,
        sentinel_health=sentinel_health,
    )

    # Apply verdict to state
    current_state.status = verdict.status
    current_state.health = verdict.health
    current_state.status_reason = verdict.reason
    current_state.last_reconciled_at = _now()
    current_state.state_fingerprint = _fingerprint(current_state)

    # Determine what changed
    status_changed = previous_state is None or previous_state.status != current_state.status
    health_changed = previous_state is None or previous_state.health != current_state.health
    reason_changed = previous_state is None or previous_state.status_reason != current_state.status_reason
    state_changed = status_changed or health_changed or reason_changed

    # Build outbox events
    outbox_events: list[OutboxEvent] = []

    if status_changed or health_changed:
        outbox_events.append(
            OutboxEvent(
                lifecycle_id=lifecycle_id,
                event_type="bloodbank.v1.lifecycle.status.updated",
                payload={
                    "lifecycle_id": lifecycle_id,
                    "repo": "",  # filled by DB layer
                    "previous": _state_to_json(previous_state) if previous_state else None,
                    "current": _state_to_json(current_state),
                    "transition": {
                        "reason": verdict.reason,
                        "computed": True,
                        "detector": "lifecycle-controller@0.1.0",
                    },
                    "blockers": [_blocker_to_json(b) for b in verdict.blockers],
                    "signals": _signals_to_json(verdict.signals),
                },
            )
        )

    # Blocker deltas
    blockers_delta: list[dict] = []
    if previous_state:
        # We don't have previous blockers in state; DB layer would need to track
        # For now, emit detected for all active blockers on first reconcile
        pass

    for blocker in verdict.blockers:
        blockers_delta.append({"action": "detected", "blocker": blocker})
        outbox_events.append(
            OutboxEvent(
                lifecycle_id=lifecycle_id,
                event_type="bloodbank.v1.lifecycle.blocker.detected",
                payload={
                    "lifecycle_id": lifecycle_id,
                    "blocker": _blocker_to_json(blocker),
                },
            )
        )

    return ReconcileResult(
        lifecycle_id=lifecycle_id,
        previous_state=previous_state,
        current_state=current_state,
        state_changed=state_changed,
        status_changed=status_changed,
        health_changed=health_changed,
        blockers_delta=blockers_delta,
        outbox_events=outbox_events,
    )


def _state_to_json(state: LifecycleState) -> dict:
    return {
        "status": state.status.value,
        "health": state.health.value,
        "phase": state.phase,
        "progress_percent": state.progress_percent,
    }


def _blocker_to_json(blocker: Blocker) -> dict:
    return {
        "id": blocker.id,
        "kind": blocker.kind.value,
        "scope": blocker.scope,
        "blocking": blocker.blocking,
        "summary": blocker.summary,
        "owner_kind": blocker.owner_kind,
        "owner_id": blocker.owner_id,
    }


def _signals_to_json(signals: LifecycleSignals) -> dict:
    return {
        "open_work_items": signals.open_work_items,
        "runnable_work_items": signals.runnable_work_items,
        "active_agent_runs": signals.active_agent_runs,
        "open_blockers": signals.open_blockers,
        "last_progress_at": signals.last_progress_at.isoformat() if signals.last_progress_at else None,
    }
