-- Migration: lifecycle-controller tables
-- Apply after candystore migrations (shares postgres instance)

\c candystore;

-- Core lifecycle registry
CREATE TABLE IF NOT EXISTS lifecycles (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    repo            TEXT NOT NULL,
    repos           TEXT,
    roadmap_id      TEXT,
    status          TEXT NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'active', 'waiting', 'blocked', 'paused', 'disabled', 'completed', 'canceled', 'archived')),
    health          TEXT NOT NULL DEFAULT 'nominal'
        CHECK (health IN ('nominal', 'at_risk', 'stalled', 'degraded', 'blocked')),
    phase           TEXT,
    progress_percent REAL DEFAULT 0 CHECK (progress_percent BETWEEN 0 AND 100),
    roadmap_version  INTEGER DEFAULT 1,
    created_by      TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    policy          JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_lifecycles_repo ON lifecycles(repo);
CREATE INDEX IF NOT EXISTS idx_lifecycles_status ON lifecycles(status);

-- Materialized current state
CREATE TABLE IF NOT EXISTS lifecycle_state (
    lifecycle_id        TEXT PRIMARY KEY REFERENCES lifecycles(id) ON DELETE CASCADE,
    status              TEXT NOT NULL,
    health              TEXT NOT NULL,
    phase               TEXT,
    progress_percent    REAL DEFAULT 0 CHECK (progress_percent BETWEEN 0 AND 100),
    roadmap_version     INTEGER DEFAULT 1,
    status_reason       TEXT,
    health_reason       TEXT,
    last_progress_at    TIMESTAMPTZ,
    last_reconciled_at  TIMESTAMPTZ DEFAULT now(),
    state_version       INTEGER DEFAULT 1,
    state_fingerprint   TEXT,
    updated_at          TIMESTAMPTZ DEFAULT now(),
    policy              JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_state_status ON lifecycle_state(status);
CREATE INDEX IF NOT EXISTS idx_lifecycle_state_last_reconciled ON lifecycle_state(last_reconciled_at);

-- Status history (append-only)
CREATE TABLE IF NOT EXISTS lifecycle_status_history (
    id              BIGSERIAL PRIMARY KEY,
    lifecycle_id    TEXT NOT NULL REFERENCES lifecycles(id) ON DELETE CASCADE,
    status          TEXT NOT NULL,
    health          TEXT NOT NULL,
    phase           TEXT,
    progress_percent REAL,
    roadmap_version  INTEGER,
    status_reason    TEXT,
    transition       JSONB,
    blockers         JSONB,
    signals          JSONB,
    policy           JSONB,
    reconciled_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_status_history_lifecycle ON lifecycle_status_history(lifecycle_id, reconciled_at DESC);

-- Blockers
CREATE TABLE IF NOT EXISTS lifecycle_blockers (
    id              TEXT PRIMARY KEY,
    lifecycle_id    TEXT NOT NULL REFERENCES lifecycles(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL
        CHECK (kind IN (
            'missing_human_input', 'human_review_required', 'dependency_not_ready',
            'dependency_failed', 'ci_failing', 'tests_failing', 'merge_conflict',
            'agent_run_failed', 'agent_idle', 'scheduler_down', 'auth_required',
            'credential_missing', 'environment_unavailable', 'rate_limited',
            'scope_ambiguous', 'acceptance_criteria_missing', 'planning_gap',
            'ticket_state_inconsistent', 'repository_locked', 'repository_dirty'
        )),
    scope           TEXT NOT NULL DEFAULT 'lifecycle'
        CHECK (scope IN ('lifecycle', 'ticket', 'repo', 'agent', 'system')),
    blocking        BOOLEAN NOT NULL DEFAULT true,
    summary         TEXT,
    owner_kind      TEXT CHECK (owner_kind IN ('human', 'agent', 'system', 'service')),
    owner_id        TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    fingerprint     TEXT
);

CREATE INDEX IF NOT EXISTS idx_blockers_lifecycle ON lifecycle_blockers(lifecycle_id, resolved_at);
CREATE INDEX IF NOT EXISTS idx_blockers_kind ON lifecycle_blockers(kind);

-- Gates
CREATE TABLE IF NOT EXISTS lifecycle_gates (
    id              TEXT PRIMARY KEY,
    lifecycle_id    TEXT NOT NULL REFERENCES lifecycles(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL
        CHECK (kind IN ('human_review', 'ci_gate', 'approval', 'security_review', 'custom')),
    blocking        BOOLEAN NOT NULL DEFAULT true,
    reason          TEXT,
    continue_policy TEXT NOT NULL DEFAULT 'hold_until_resolved'
        CHECK (continue_policy IN ('hold_until_resolved', 'continue_parallel_work', 'auto_resolve_after_sla')),
    owner_kind      TEXT CHECK (owner_kind IN ('human', 'agent', 'system', 'service')),
    owner_id        TEXT,
    sla_due_at      TIMESTAMPTZ,
    triggered_by_checkpoint_id TEXT,
    opened_at       TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    resolution      TEXT CHECK (resolution IN ('approved', 'rejected', 'bypassed', 'auto_resolved', 'superseded'))
);

CREATE INDEX IF NOT EXISTS idx_gates_lifecycle ON lifecycle_gates(lifecycle_id, resolved_at);

-- Checkpoints
CREATE TABLE IF NOT EXISTS lifecycle_checkpoints (
    id              TEXT PRIMARY KEY,
    lifecycle_id    TEXT NOT NULL REFERENCES lifecycles(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL
        CHECK (kind IN ('mvp', 'phase', 'milestone', 'release', 'custom')),
    name            TEXT NOT NULL,
    roadmap_version INTEGER NOT NULL DEFAULT 1,
    phase_id        TEXT,
    reached_at      TIMESTAMPTZ,
    invalidated_at  TIMESTAMPTZ,
    evidence        JSONB
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_lifecycle ON lifecycle_checkpoints(lifecycle_id, reached_at);

-- Observations (TTL'd raw facts)
CREATE TABLE IF NOT EXISTS lifecycle_observations (
    id              BIGSERIAL PRIMARY KEY,
    lifecycle_id    TEXT NOT NULL REFERENCES lifecycles(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    observed_at     TIMESTAMPTZ DEFAULT now(),
    expires_at      TIMESTAMPTZ DEFAULT (now() + interval '24 hours'),
    payload         JSONB NOT NULL,
    payload_hash    TEXT,
    confidence      REAL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1)
);

CREATE INDEX IF NOT EXISTS idx_observations_lifecycle ON lifecycle_observations(lifecycle_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_expires ON lifecycle_observations(expires_at);

-- Reconcile queue (dirty-queue + lease)
CREATE TABLE IF NOT EXISTS lifecycle_reconcile_queue (
    lifecycle_id        TEXT PRIMARY KEY REFERENCES lifecycles(id) ON DELETE CASCADE,
    reason              TEXT NOT NULL DEFAULT 'periodic_sweep',
    priority            INTEGER DEFAULT 0,
    available_at        TIMESTAMPTZ DEFAULT now(),
    attempts            INTEGER DEFAULT 0,
    leased_by           TEXT,
    lease_expires_at    TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reconcile_queue_available ON lifecycle_reconcile_queue(available_at, priority DESC)
    WHERE leased_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_reconcile_queue_lease ON lifecycle_reconcile_queue(lease_expires_at)
    WHERE leased_by IS NOT NULL;

-- Event outbox (transactional outbox)
CREATE TABLE IF NOT EXISTS lifecycle_event_outbox (
    id              BIGSERIAL PRIMARY KEY,
    lifecycle_id    TEXT NOT NULL REFERENCES lifecycles(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL,
    headers         JSONB,
    created_at      TIMESTAMPTZ DEFAULT now(),
    published_at    TIMESTAMPTZ,
    publish_attempts INTEGER DEFAULT 0,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_unpublished ON lifecycle_event_outbox(published_at, created_at)
    WHERE published_at IS NULL;

-- Sentinel heartbeats
CREATE TABLE IF NOT EXISTS sentinel_heartbeats (
    sentinel_id             TEXT PRIMARY KEY,
    scope_kind              TEXT NOT NULL DEFAULT 'global'
        CHECK (scope_kind IN ('global', 'repo', 'lifecycle', 'service')),
    scope_id                TEXT,
    last_seen_at            TIMESTAMPTZ DEFAULT now(),
    last_successful_scan_at TIMESTAMPTZ,
    last_error_at           TIMESTAMPTZ,
    status                  TEXT NOT NULL DEFAULT 'healthy'
        CHECK (status IN ('healthy', 'degraded', 'missing', 'error')),
    error_summary           TEXT,
    updated_at              TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_heartbeats_status ON sentinel_heartbeats(status, last_seen_at);
