"""Domain models for the lifecycle controller.

Pure dataclasses — no DB, no I/O. These are the shapes the reconciler
works with. DB layer translates to/from these.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class LifecycleStatus(str, Enum):
    PLANNED = "planned"
    ACTIVE = "active"
    WAITING = "waiting"
    BLOCKED = "blocked"
    PAUSED = "paused"
    DISABLED = "disabled"
    COMPLETED = "completed"
    CANCELED = "canceled"
    ARCHIVED = "archived"


class LifecycleHealth(str, Enum):
    NOMINAL = "nominal"
    AT_RISK = "at_risk"
    STALLED = "stalled"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class BlockerKind(str, Enum):
    MISSING_HUMAN_INPUT = "missing_human_input"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    DEPENDENCY_NOT_READY = "dependency_not_ready"
    DEPENDENCY_FAILED = "dependency_failed"
    CI_FAILING = "ci_failing"
    TESTS_FAILING = "tests_failing"
    MERGE_CONFLICT = "merge_conflict"
    AGENT_RUN_FAILED = "agent_run_failed"
    AGENT_IDLE = "agent_idle"
    SCHEDULER_DOWN = "scheduler_down"
    AUTH_REQUIRED = "auth_required"
    CREDENTIAL_MISSING = "credential_missing"
    ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
    RATE_LIMITED = "rate_limited"
    SCOPE_AMBIGUOUS = "scope_ambiguous"
    ACCEPTANCE_CRITERIA_MISSING = "acceptance_criteria_missing"
    PLANNING_GAP = "planning_gap"
    TICKET_STATE_INCONSISTENT = "ticket_state_inconsistent"
    REPOSITORY_LOCKED = "repository_locked"
    REPOSITORY_DIRTY = "repository_dirty"


class GateKind(str, Enum):
    HUMAN_REVIEW = "human_review"
    CI_GATE = "ci_gate"
    APPROVAL = "approval"
    SECURITY_REVIEW = "security_review"
    CUSTOM = "custom"


class GatePolicy(str, Enum):
    HOLD_UNTIL_RESOLVED = "hold_until_resolved"
    CONTINUE_PARALLEL_WORK = "continue_parallel_work"
    AUTO_RESOLVE_AFTER_SLA = "auto_resolve_after_sla"


class GateResolution(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    BYPASSED = "bypassed"
    AUTO_RESOLVED = "auto_resolved"
    SUPERSEDED = "superseded"


class CheckpointKind(str, Enum):
    MVP = "mvp"
    PHASE = "phase"
    MILESTONE = "milestone"
    RELEASE = "release"
    CUSTOM = "custom"


@dataclass
class Blocker:
    id: str
    kind: BlockerKind
    lifecycle_id: str = ""
    scope: str = "lifecycle"
    blocking: bool = True
    summary: str = ""
    owner_kind: str | None = None
    owner_id: str | None = None
    created_at: datetime | None = None


@dataclass
class Gate:
    id: str
    kind: GateKind
    blocking: bool = True
    reason: str = ""
    continue_policy: GatePolicy = GatePolicy.HOLD_UNTIL_RESOLVED
    owner_kind: str | None = None
    owner_id: str | None = None
    sla_due_at: datetime | None = None
    triggered_by_checkpoint_id: str | None = None
    opened_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution: GateResolution | None = None


@dataclass
class Checkpoint:
    id: str
    kind: CheckpointKind
    name: str
    roadmap_version: int = 1
    phase_id: str | None = None
    reached_at: datetime | None = None
    invalidated_at: datetime | None = None
    evidence: list[dict] = field(default_factory=list)


@dataclass
class LifecyclePolicy:
    progress_expected: bool = True
    stalled_after_minutes: int = 90
    blocked_after_minutes: int = 15
    observer_stale_after_minutes: int = 10
    sentinel_missing_after_minutes: int = 15
    reconcile_interval_minutes: int = 3
    alerts_enabled: bool = True
    silence_if_status_in: list[str] = field(default_factory=lambda: ["paused", "disabled", "completed", "canceled"])

    @classmethod
    def from_json(cls, data: dict | None) -> "LifecyclePolicy":
        if not data:
            return cls()
        return cls(
            progress_expected=data.get("progress_expected", True),
            stalled_after_minutes=data.get("stalled_after_minutes", 90),
            blocked_after_minutes=data.get("blocked_after_minutes", 15),
            observer_stale_after_minutes=data.get("observer_stale_after_minutes", 10),
            sentinel_missing_after_minutes=data.get("sentinel_missing_after_minutes", 15),
            reconcile_interval_minutes=data.get("reconcile_interval_minutes", 3),
            alerts_enabled=data.get("alerts_enabled", True),
            silence_if_status_in=data.get("silence_if_status_in", ["paused", "disabled", "completed", "canceled"]),
        )

    def to_json(self) -> dict:
        return {
            "progress_expected": self.progress_expected,
            "stalled_after_minutes": self.stalled_after_minutes,
            "blocked_after_minutes": self.blocked_after_minutes,
            "observer_stale_after_minutes": self.observer_stale_after_minutes,
            "sentinel_missing_after_minutes": self.sentinel_missing_after_minutes,
            "reconcile_interval_minutes": self.reconcile_interval_minutes,
            "alerts_enabled": self.alerts_enabled,
            "silence_if_status_in": self.silence_if_status_in,
        }


@dataclass
class LifecycleState:
    lifecycle_id: str
    status: LifecycleStatus
    health: LifecycleHealth
    phase: str | None = None
    progress_percent: float = 0.0
    roadmap_version: int = 1
    status_reason: str = ""
    health_reason: str = ""
    last_progress_at: datetime | None = None
    last_reconciled_at: datetime | None = None
    state_version: int = 1
    state_fingerprint: str = ""
    policy: LifecyclePolicy = field(default_factory=LifecyclePolicy)


@dataclass
class LifecycleSignals:
    open_work_items: int = 0
    runnable_work_items: int = 0
    active_agent_runs: int = 0
    open_blockers: int = 0
    last_progress_at: datetime | None = None


@dataclass
class LifecycleVerdict:
    status: LifecycleStatus
    health: LifecycleHealth
    reason: str
    blockers: list[Blocker] = field(default_factory=list)
    signals: LifecycleSignals = field(default_factory=LifecycleSignals)


@dataclass
class Observation:
    id: int | None = None
    lifecycle_id: str = ""
    source: str = ""  # e.g. 'plane-sentinel', 'git-sentinel'
    kind: str = ""    # e.g. 'work_items_snapshot', 'repo_activity_snapshot'
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    payload: dict = field(default_factory=dict)
    payload_hash: str | None = None
    confidence: float = 1.0


@dataclass
class OutboxEvent:
    id: int | None = None
    lifecycle_id: str = ""
    event_type: str = ""
    payload: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    created_at: datetime | None = None
    published_at: datetime | None = None
    publish_attempts: int = 0
    error: str | None = None
