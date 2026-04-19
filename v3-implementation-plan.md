# Bloodbank v3 implementation plan

This document is the execution plan for the Bloodbank v3 refactor. It is meant
to be followed by a mixed-seniority team without reinterpreting the architecture
on every ticket.

Use this plan as the source of truth for the `v3-refactor` Plane epic and its
child tickets.

## Current baseline

The starting point is the Bloodbank submodule on branch `v3-refactor`, created
from commit `14d9122`. The parent 33GOD repository already references that
submodule commit, and the submodule has uncommitted cleanup deletions from the
repository pruning pass.

Do not revert the cleanup deletions unless the user explicitly asks. New v3 work
must go into clearly named v3 paths so it does not depend on the removed legacy
files.

## Source documents

Read these files before editing code:

- `docs/architecture/bloodbank-vnext.md`
- `docs/architecture/dapr-vs-faststream.md`
- `docs/architecture/overhaul-backlog.md`
- `GOD.md`
- `README.md`
- `AGENTS.md`

The vNext docs define the target architecture. `GOD.md` and `README.md`
describe the current v2 stack and are useful mainly for migration and
retirement context.

## Non-negotiable architecture decisions

These decisions are already made. Do not reopen them inside implementation
tickets.

- Use Dapr as the runtime platform.
- Use NATS JetStream as the broker.
- Use CloudEvents 1.0 for immutable event envelopes.
- Use a separate command envelope for mutable commands.
- Use AsyncAPI as the contract description format.
- Use EventCatalog for human and agent-facing discovery.
- Use Apicurio Registry for runtime schema governance.
- Keep Docker Compose as the default local and self-hosted deployment model.
- Keep the Bloodbank CLI, but make it an operator tool rather than the primary
  production publish path.
- Keep Holyfields as the central contract and service registry.
- Do not let Bloodbank own business event schemas.

## Repository boundaries

This plan intentionally separates Bloodbank and Holyfields.

Bloodbank owns runtime and operations:

- Dapr component manifests.
- NATS JetStream bootstrap.
- Compose files for the platform sandbox.
- Operator CLI scaffolding.
- Adapter migration points.
- Replay, trace, and dead-letter tools.
- Documentation that explains how to operate v3.

Holyfields owns contracts and generation:

- CloudEvents base schema.
- Command envelope schema.
- Service-level AsyncAPI documents.
- Service-owned event and command schemas.
- Generated Python and TypeScript SDKs.
- EventCatalog source and generated catalog output.
- Apicurio schema synchronization.

If a ticket requires editing `../holyfields`, do that work in the Holyfields
repository or a dedicated Holyfields ticket. Do not hide Holyfields changes
inside Bloodbank.

## Target Bloodbank v3 layout

The v3 scaffold in this repo must use these paths.

```text
bloodbank/
  compose/
    v3/
      docker-compose.yml
      README.md
      components/
        pubsub.yaml
        statestore.yaml
        secretstore.yaml
      nats/
        README.md
        streams.json
      apicurio/
        README.md
      eventcatalog/
        README.md
  ops/
    v3/
      bootstrap/
        README.md
        check-platform.sh
      trace/
        README.md
      replay/
        README.md
  cli/
    v3/
      README.md
      bb_v3.py
  adapters/
    v3/
      README.md
      hookd/
        README.md
      openclaw/
        README.md
      infra_dispatcher/
        README.md
  docs/
    architecture/
      bloodbank-vnext.md
      dapr-vs-faststream.md
      overhaul-backlog.md
  v3-implementation-plan.md
```

The first scaffold must be documentation-heavy and safe. Do not wire production
traffic to Dapr or NATS in the first pass.

## Naming rules

Use these names consistently:

- Plane epic title: `v3-refactor`.
- Branch name: `v3-refactor`.
- Compose project name: `bloodbank-v3`.
- Dapr pub/sub component name: `bloodbank-v3-pubsub`.
- Dapr app ID prefix: `bloodbank-v3-`.
- NATS stream for immutable events: `BLOODBANK_V3_EVENTS`.
- NATS stream for commands: `BLOODBANK_V3_COMMANDS`.
- NATS subject prefix for events: `event.`
- NATS subject prefix for commands: `command.`
- NATS subject prefix for replies: `reply.`

## Message contracts

Bloodbank must not invent business schemas. The temporary local examples below
exist only to prove platform plumbing until Holyfields generated packages are
available.

Immutable event envelope:

```json
{
  "specversion": "1.0",
  "id": "uuid",
  "source": "urn:33god:service:example",
  "type": "artifact.created",
  "subject": "artifact/example-id",
  "time": "2026-04-12T00:00:00Z",
  "datacontenttype": "application/json",
  "dataschema": "urn:33god:holyfields:schema:artifact.created.v1",
  "correlationid": "uuid",
  "causationid": "uuid-or-null",
  "producer": "example-service",
  "service": "artifact-service",
  "domain": "artifact",
  "schemaref": "artifact.created.v1",
  "traceparent": "w3c-trace-context",
  "data": {}
}
```

Command envelope:

```json
{
  "command_id": "uuid",
  "command_type": "artifact.rebuild",
  "target_service": "artifact-service",
  "issued_by": "operator-or-service",
  "issued_at": "2026-04-12T00:00:00Z",
  "timeout_ms": 300000,
  "correlation_id": "uuid",
  "causation_id": "uuid-or-null",
  "reply_to": "reply.artifact-service.rebuild",
  "payload_schema": "artifact.rebuild.v1",
  "payload": {}
}
```

## Developer workflow

Use this workflow for every ticket.

1. Read this plan and the ticket acceptance criteria.
2. Confirm the current branch is `v3-refactor`.
3. Confirm the ticket path scope before editing.
4. Make only the files required by the ticket.
5. Run the ticket-specific verification command.
6. Run a local self-review.
7. Ask for spec review.
8. Ask for code quality review.
9. Mark the ticket complete only after both reviews pass.

Do not mark a ticket complete if verification was skipped.

## Verification commands

Use these commands during the first scaffold wave.

```bash
git status --short
python -m compileall cli/v3
bash ops/v3/bootstrap/check-platform.sh
```

`bash ops/v3/bootstrap/check-platform.sh` must not require Docker to be running.
It must validate file presence and static configuration only.

Once the Compose stack exists, add this optional manual check:

```bash
docker compose -f compose/v3/docker-compose.yml config
```

Do not require network access or pulling images for the baseline verification.

## Plane ticket set

Create one parent Plane issue and the child issues listed below in the
`33god/bloodbank` Plane project.

### Epic

Title: `v3-refactor`

Description:

```text
Refactor Bloodbank into the v3 event platform: Dapr runtime, NATS JetStream
broker, CloudEvents immutable event envelope, separate command envelope,
AsyncAPI/Holyfields contract source of truth, EventCatalog discovery, Apicurio
schema registry, Docker Compose self-hosted deployment, and operator-focused
Bloodbank CLI.
```

Acceptance criteria:

- v3 platform scaffold exists in Bloodbank.
- Holyfields base contract work is tracked separately and linked.
- One reference vertical slice can publish and consume through the v3 platform.
- Old v2 publish paths have a documented retirement plan.

### Child tickets

#### V3-001: Sync Bloodbank submodule baseline

Scope: create a clean branch and record the baseline.

Implementation steps:

1. Confirm the submodule branch is `v3-refactor`.
2. Record the starting commit and known dirty cleanup set in the ticket.
3. Do not revert the user cleanup deletions.

Acceptance criteria:

- Branch is `v3-refactor`.
- The ticket links to `v3-implementation-plan.md`.
- No legacy cleanup deletions were reverted.

#### V3-002: Create v3 Compose scaffold

Scope: create `compose/v3/`.

Implementation steps:

1. Add `compose/v3/docker-compose.yml`.
2. Add `compose/v3/README.md`.
3. Add placeholder component directories for Dapr, NATS, Apicurio, and
   EventCatalog.
4. Use service names that match the naming rules in this plan.

Acceptance criteria:

- `docker compose -f compose/v3/docker-compose.yml config` parses.
- The Compose file defines `nats`, `dapr-placement`, `apicurio-registry`, and
  `eventcatalog` services.
- The scaffold does not depend on current v2 RabbitMQ services.

#### V3-003: Add Dapr component manifests

Scope: create static Dapr component manifests.

Implementation steps:

1. Add `compose/v3/components/pubsub.yaml`.
2. Add `compose/v3/components/statestore.yaml`.
3. Add `compose/v3/components/secretstore.yaml`.
4. Use `bloodbank-v3-pubsub` as the pub/sub component name.

Acceptance criteria:

- The pub/sub manifest targets NATS.
- The manifests use placeholder-safe local values only.
- The manifests are documented in `compose/v3/README.md`.

#### V3-004: Define NATS JetStream topology

Scope: define stream names, subjects, retention notes, and replay posture.

Implementation steps:

1. Add `compose/v3/nats/streams.json`.
2. Add `compose/v3/nats/README.md`.
3. Define `BLOODBANK_V3_EVENTS` and `BLOODBANK_V3_COMMANDS`.
4. Define `event.>`, `command.>`, and `reply.>` subject conventions.

Acceptance criteria:

- The topology matches this plan's naming rules.
- The README explains replay and dead-letter assumptions.
- The topology is not coupled to RabbitMQ.

#### V3-005: Add operator CLI v3 skeleton

Scope: create a safe CLI scaffold without production side effects.

Implementation steps:

1. Add `cli/v3/README.md`.
2. Add `cli/v3/bb_v3.py`.
3. Implement command stubs for `doctor`, `trace`, `replay`, and `emit`.
4. Make `doctor` perform static local checks only.

Acceptance criteria:

- `python -m compileall cli/v3` passes.
- `python cli/v3/bb_v3.py doctor` exits successfully when scaffold files
  exist.
- The CLI does not publish network traffic.

#### V3-006: Add platform bootstrap check

Scope: create a static verification script for junior developers.

Implementation steps:

1. Add `ops/v3/bootstrap/README.md`.
2. Add `ops/v3/bootstrap/check-platform.sh`.
3. Check for required v3 scaffold files.
4. Print clear pass or fail messages.

Acceptance criteria:

- `bash ops/v3/bootstrap/check-platform.sh` passes after scaffold files exist.
- The script does not require Docker, Dapr, NATS, or network access.
- Missing files produce actionable error messages.

#### V3-007: Add replay and trace docs

Scope: document the operator workflows before implementing real replay.

Implementation steps:

1. Add `ops/v3/replay/README.md`.
2. Add `ops/v3/trace/README.md`.
3. Define correlation ID, causation ID, and `traceparent` expectations.
4. Define replay safety rules.

Acceptance criteria:

- The docs explain what data is safe to replay.
- The docs explain that replays preserve original IDs and add replay metadata.
- The docs do not claim production replay is implemented yet.

#### V3-008: Add adapter migration scaffolds

Scope: prepare the bridge migration directories.

Implementation steps:

1. Add `adapters/v3/README.md`.
2. Add `adapters/v3/hookd/README.md`.
3. Add `adapters/v3/openclaw/README.md`.
4. Add `adapters/v3/infra_dispatcher/README.md`.
5. Document that adapters map external payloads into Holyfields-generated
   contracts, publish through Dapr and NATS, and must not invent local
   envelopes.

Acceptance criteria:

- Each adapter README names the current v2 component it will replace.
- Each adapter README names the target v3 publish path through Holyfields
  contracts, Dapr, and NATS.
- No adapter contains executable migration code yet.

#### V3-009: Link the implementation plan from architecture docs

Scope: make the plan discoverable.

Implementation steps:

1. Link `v3-implementation-plan.md` from `README.md`.
2. Link `v3-implementation-plan.md` from `docs/architecture/README.md`.
3. Link `v3-implementation-plan.md` from `docs/architecture/overhaul-backlog.md`.

Acceptance criteria:

- All links resolve locally.
- The v2 `GOD.md` remains clearly marked as legacy/current-state context.
- The architecture index also links the Holyfields contract work tracker.

#### V3-010: Create Holyfields contract work tracker

Scope: document the sibling-repo work that must happen outside Bloodbank.

Implementation steps:

1. Add or update the dedicated tracker at
   [docs/architecture/v3-holyfields-contract-work.md](docs/architecture/v3-holyfields-contract-work.md).
2. List required Holyfields outputs: base event schema, command envelope schema,
   AsyncAPI template, SDK generation, catalog generation, Apicurio sync.
3. Mark those tasks as external to the Bloodbank repo.
4. Link the tracker from
   [docs/architecture/README.md](docs/architecture/README.md).

Acceptance criteria:

- No Holyfields code is edited in the Bloodbank submodule.
- Bloodbank tickets clearly identify which work is blocked on Holyfields.
- The tracker exists at `docs/architecture/v3-holyfields-contract-work.md`.

#### V3-011: Verify first scaffold wave

Scope: run the documented static checks and inspect results.

Implementation steps:

1. Run `git status --short`.
2. Run `python -m compileall cli/v3`.
3. Run `bash ops/v3/bootstrap/check-platform.sh`.
4. Run `docker compose -f compose/v3/docker-compose.yml config` if Docker
   Compose is available and does not require pulling images.

Acceptance criteria:

- Static checks pass.
- Any skipped Docker check is documented with the reason.
- Ticket completion is blocked until spec review and code quality review pass.

## Subagent orchestration protocol

Use subagent-driven development for the scaffold wave.

Controller rules:

- Dispatch a fresh implementer subagent per ticket.
- Do not dispatch multiple implementer subagents in parallel.
- Give each subagent the full ticket text from this plan.
- Give each subagent the exact allowed write paths.
- Require each implementer to self-review before returning.
- Dispatch a spec reviewer after implementation.
- Dispatch a code quality reviewer only after spec review passes.
- If a reviewer finds issues, send the issue list back to the implementer and
  re-review.
- Mark a Plane ticket complete only after both review stages pass.

For the first session, scaffold tickets may be grouped only when their write
sets are disjoint and the controller can still review them thoroughly. The safe
first grouping is:

- Group A: `V3-002`, `V3-003`, and `V3-004` for platform files under
  `compose/v3/`.
- Group B: `V3-005` and `V3-006` for operator CLI and bootstrap checks under
  `cli/v3/` and `ops/v3/bootstrap/`.
- Group C: `V3-007`, `V3-008`, `V3-009`, and `V3-010` for docs and adapter
  scaffolds.

Do not group `V3-011`; it is a verification gate after the scaffold groups.

## Stop conditions

Stop and ask for direction if any of these happen:

- A ticket requires editing `../holyfields` from the Bloodbank submodule.
- A migration would route live production traffic to Dapr or NATS.
- The cleanup delete set needs to be reverted.
- Plane API creation fails after authentication and endpoint checks.
- Docker Compose requires pulling images during a static verification step.

## Sprint one definition of done

Sprint one is done when all of these are true:

- The `v3-refactor` Plane epic exists.
- All child tickets in this plan exist under that epic.
- The Bloodbank v3 scaffold exists under the target paths.
- The operator CLI and bootstrap check compile or run locally.
- The implementation plan is linked from the architecture docs.
- Spec review and code quality review have passed for the scaffold wave.
