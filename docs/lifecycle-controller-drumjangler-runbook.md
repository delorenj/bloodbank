# Lifecycle Controller Drumjangler Live Demo Runbook

This runbook walks a first-time operator through a live demonstration of the Bloodbank lifecycle-controller using the ongoing Drumjangler MVP lifecycle as the dogfood subject.

The demo proves these implemented pieces:

1. Lifecycle database schema and migration
2. Deterministic lifecycle reconciler
3. Dirty reconcile queue
4. Lifecycle observations from sentinels
5. Blocker and gate modeling
6. Transactional event outbox
7. Drumjangler dogfood script
8. Unit/lint verification

## What the demo shows

The lifecycle controller computes aggregate project state from observations, blockers, and gates:

- `active / nominal`: Drumjangler has runnable work and progress signals.
- `blocked / blocked`: all work is waiting on human input and no legal runnable path exists.
- `active / stalled`: work is runnable, but repo progress has not moved for the configured threshold.
- `waiting / nominal`: an MVP human-review gate is intentionally holding forward motion.

## Prerequisites

Run from the Bloodbank repo:

```bash
cd ~/code/33GOD/bloodbank
```

Required local tools:

- Docker
- mise
- uv through mise: `mise x -- uv ...`

Required running container:

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep bloodbank-postgres
```

Expected: a healthy/running `bloodbank-postgres` container.

If it is missing, boot the Bloodbank sandbox first:

```bash
mise run up
```

## Step 1 — Apply the lifecycle DB schema

The dogfood demo stores its state in the Bloodbank Postgres container.

```bash
docker exec -i bloodbank-postgres \
  psql -U candystore -d candystore \
  < services/lifecycle-controller/src/db/001_lifecycle_controller.sql
```

Expected shape:

```text
CREATE TABLE
CREATE INDEX
...
```

It is safe to run repeatedly because the migration uses `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`.

Verify the main tables exist:

```bash
docker exec -i bloodbank-postgres psql -U candystore -d candystore -c "\dt lifecycle_*"
```

Expected tables include:

- `lifecycle_state`
- `lifecycle_observations`
- `lifecycle_blockers`
- `lifecycle_gates`
- `lifecycle_reconcile_queue`
- `lifecycle_event_outbox`

## Step 2 — Verify the controller code

```bash
cd services/lifecycle-controller
mise x -- uv run ruff check .
mise x -- uv run pytest -q
```

Expected current result:

```text
All checks passed!
14 passed
```

What this proves:

- The reconciler handles the core state machine.
- Latest observations win over stale observations.
- Repo activity observations can drive stalled detection.
- Drumjangler scenarios are covered by tests.

## Step 3 — Run the Drumjangler dogfood demo

```bash
PYTHONPATH=src mise x -- uv run python scripts/dogfood_drumjangler.py
```

The script resets only the dogfood lifecycle id, then runs four scenarios against live Postgres.

Expected high-level output:

```text
🥁 Drumjangler Lifecycle Controller Dogfood
🧹 Reset prior dogfood rows for lc_drumjangler_mvp
✅ Lifecycle lc_drumjangler_mvp created (or already exists)

📋 Scenario 1: Active with tickets
Current:  active / nominal
Reason:   PROGRESSING

📋 Scenario 2: All tickets waiting on human input
Current:  blocked / blocked
Reason:   NO_RUNNABLE_WORK

📋 Scenario 3: Stalled — no commits in 2 hours
Current:  active / stalled
Reason:   RUNNABLE_WORK_NOT_ADVANCING

📋 Scenario 4: MVP checkpoint reached, human review gate
Current:  waiting / nominal
Reason:   BLOCKING_GATE_OPEN

📬 Outbox (6 unpublished):
   bloodbank.v1.lifecycle.status.updated
   bloodbank.v1.lifecycle.blocker.detected
...
✅ Dogfood complete!
```

What this proves:

- The lifecycle registry and state rows can be created.
- Sentinel-like observations can be inserted.
- The reconciler computes status and health transitions.
- Blockers and gates influence aggregate verdicts.
- Lifecycle events are staged in the transactional outbox.

## Step 4 — Inspect the live lifecycle state

Return to the Bloodbank repo root if needed:

```bash
cd ~/code/33GOD/bloodbank
```

Query the current Drumjangler lifecycle state:

```bash
docker exec -i bloodbank-postgres psql -U candystore -d candystore -x -c "
SELECT lifecycle_id, status, health, status_reason, state_version, last_reconciled_at
FROM lifecycle_state
WHERE lifecycle_id = 'lc_drumjangler_mvp';
"
```

Expected after the full demo:

- `status`: `waiting`
- `health`: `nominal`
- `status_reason`: `BLOCKING_GATE_OPEN`

## Step 5 — Inspect the raw observations

```bash
docker exec -i bloodbank-postgres psql -U candystore -d candystore -x -c "
SELECT source, kind, observed_at, payload
FROM lifecycle_observations
WHERE lifecycle_id = 'lc_drumjangler_mvp'
ORDER BY observed_at DESC;
"
```

Expected examples:

- `plane-sentinel / work_items_snapshot`
- `agent-sentinel / agent_runs_snapshot`
- `git-sentinel / repo_activity_snapshot`

What this proves:

- The controller can ingest sentinel facts without knowing each sentinel implementation.
- Reconciliation is based on persisted facts, not hardcoded state.

## Step 6 — Inspect blockers

```bash
docker exec -i bloodbank-postgres psql -U candystore -d candystore -x -c "
SELECT id, kind, blocking, summary, resolved_at
FROM lifecycle_blockers
WHERE lifecycle_id = 'lc_drumjangler_mvp';
"
```

Expected:

- `blk_human_1`
- `kind`: `missing_human_input`
- `resolved_at`: set after Scenario 4 resolves the blocker before opening the MVP gate

What this proves:

- `blocked` is represented as aggregate lifecycle state.
- The named reason lives in blockers, not in ticket status names.

## Step 7 — Inspect gates

```bash
docker exec -i bloodbank-postgres psql -U candystore -d candystore -x -c "
SELECT id, kind, blocking, reason, owner_kind, owner_id, resolved_at
FROM lifecycle_gates
WHERE lifecycle_id = 'lc_drumjangler_mvp';
"
```

Expected:

- `gate_mvp_review`
- `kind`: `human_review`
- `blocking`: `true`
- `owner_id`: `jarad`
- `resolved_at`: empty/null

What this proves:

- MVP review is modeled as an intentional gate.
- `waiting / nominal` is different from `blocked / blocked`.

## Step 8 — Inspect outbox events

```bash
docker exec -i bloodbank-postgres psql -U candystore -d candystore -x -c "
SELECT id, event_type, payload, published_at
FROM lifecycle_event_outbox
WHERE lifecycle_id = 'lc_drumjangler_mvp'
ORDER BY id;
"
```

Expected event types:

- `bloodbank.v1.lifecycle.status.updated`
- `bloodbank.v1.lifecycle.blocker.detected`

What this proves:

- Reconciliation and event staging are separated.
- Status transitions are observable as Bloodbank events.
- Publishing can retry independently without losing state transitions.

Current limitation: the default `OutboxPublisher` is still a placeholder. It marks events publishable in process, but the actual NATS/Dapr publisher wiring is a follow-up item.

## Step 9 — Demonstrate repeatability

Run the dogfood script a second time:

```bash
cd ~/code/33GOD/bloodbank/services/lifecycle-controller
PYTHONPATH=src mise x -- uv run python scripts/dogfood_drumjangler.py
```

Expected:

- It starts with `🧹 Reset prior dogfood rows`.
- It produces the same four scenario transitions.
- It does not require manual DB cleanup.

What this proves:

- The demo is safe to repeat for a live walkthrough.
- The dogfood lifecycle is isolated to `lc_drumjangler_mvp`.

## Troubleshooting

### `uv: command not found`

Use mise-managed uv:

```bash
mise x -- uv run pytest -q
```

Do not call bare `uv` on this machine.

### `relation "lifecycles" does not exist`

Apply the schema:

```bash
docker exec -i bloodbank-postgres \
  psql -U candystore -d candystore \
  < services/lifecycle-controller/src/db/001_lifecycle_controller.sql
```

### `No such container: bloodbank-postgres`

Boot the Bloodbank sandbox:

```bash
cd ~/code/33GOD/bloodbank
mise run up
```

Then re-check:

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep bloodbank-postgres
```

### Scenario 2 does not become `blocked / blocked`

Run tests first:

```bash
cd ~/code/33GOD/bloodbank/services/lifecycle-controller
mise x -- uv run pytest tests/test_reconciler.py::TestEvaluateLifecycle::test_latest_observation_per_kind_wins -q
```

This catches stale observation ordering bugs.

### Scenario 3 does not become `active / stalled`

Run:

```bash
mise x -- uv run pytest tests/test_reconciler.py::TestEvaluateLifecycle::test_repo_activity_observation_can_drive_stalled_verdict -q
```

This catches missing repo-activity based stalled detection.

## Demo closeout checklist

The live demo is complete when all are true:

- `ruff check .` passes.
- `pytest -q` passes.
- `dogfood_drumjangler.py` completes.
- Final state query shows `waiting / nominal` with `BLOCKING_GATE_OPEN`.
- Observations table contains Plane/agent/git sentinel snapshots.
- Blockers table contains a resolved `missing_human_input` blocker.
- Gates table contains an open blocking human-review gate.
- Outbox table contains lifecycle status events.

## Follow-up wiring not yet in this runbook

These are not required for the current live demo, but are the next integration steps:

1. Replace `OutboxPublisher._default_publish` with the real Bloodbank NATS/Dapr publisher.
2. Add the lifecycle-controller to Docker Compose with its own Dapr sidecar if needed.
3. Wire real Drumjangler sentinels to emit observations instead of using the dogfood script.
4. Add UI views in Candybar for lifecycle status, health, blockers, gates, and outbox events.
