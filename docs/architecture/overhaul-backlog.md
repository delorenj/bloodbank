# Bloodbank vNext overhaul backlog

This document turns the vNext blueprint into an execution-ready work plan. Use
it to open tickets, assign owners, and run the first overhaul waves without
reinterpreting the architecture on every thread.

Read [bloodbank-vnext.md](bloodbank-vnext.md) first. Then read
[v3-implementation-plan.md](../../v3-implementation-plan.md) for the exact
ticket definitions and subagent execution protocol. This backlog inherits that
design and does not replace it. For the Holyfields-owned contract work that
Bloodbank depends on, also read [v3-holyfields-contract-work.md](v3-holyfields-contract-work.md).

## Operating model

Run the overhaul as six parallel workstreams with one clear owner per stream.
Each workstream can have multiple contributors, but one owner is accountable for
scope, sequencing, and acceptance.

Suggested ownership model:

- Holyfields contracts owner
- Bloodbank platform owner
- SDK and developer experience owner
- Adapter migration owner
- Observability owner
- Cutover owner

## Phase structure

Use these phases to organize the first waves of work.

### Phase 0

Goal: freeze scope and establish the inventory baseline.

### Phase 1

Goal: build the Holyfields contract system and the Bloodbank platform sandbox.

### Phase 2

Goal: prove one real reference slice end-to-end.

### Phase 3

Goal: migrate the first-party adapters and remove direct publish paths.

### Phase 4

Goal: cut over production paths and retire v2 components.

## First-wave tickets

These are the first tickets I would open.

### Holyfields contracts

1. `HF-001` Create the base CloudEvents contract package.
   Definition of done: one reusable base event schema, extension field policy,
   and examples for at least two domains.
2. `HF-002` Create the base command envelope contract package.
   Definition of done: required fields, lifecycle fields, timeout policy, and
   reply conventions are documented and validated.
3. `HF-003` Create the first service-level AsyncAPI document template.
   Definition of done: every service can copy one template and fill in events,
   commands, channels, and owners.
4. `HF-004` Add compatibility checks to Holyfields CI.
   Definition of done: breaking schema or AsyncAPI changes fail CI.
5. `HF-005` Generate Python and TypeScript SDKs from Holyfields.
   Definition of done: one Python package and one TypeScript package can be
   installed by consumers without manual code generation.

### Bloodbank platform

6. `BB-001` Create the Compose sandbox for Dapr, NATS, Apicurio, and
   EventCatalog.
   Definition of done: one `docker compose up` brings up the platform locally.
7. `BB-002` Add Dapr component manifests for pub/sub and baseline secrets or
   config.
   Definition of done: one sample service can publish and consume in the local
   stack using Dapr sidecars.
8. `BB-003` Define the NATS JetStream topology.
   Definition of done: streams, consumers, retention, replay policy, and
   dead-letter handling are codified and reviewed.
9. `BB-004` Build the new `bb` operator CLI skeleton.
   Definition of done: `bb doctor`, `bb trace`, `bb replay`, and `bb emit` have
   command stubs and one working vertical command.

### Reference slice

10. `RF-001` Pick one reference domain and map its current producers and
    consumers.
    Definition of done: one domain inventory exists with old publish paths,
    target contracts, and cutover notes.
11. `RF-002` Implement one command, one reply, and two events for the reference
    domain.
    Definition of done: the contracts exist in Holyfields and publish or consume
    in the Compose sandbox.
12. `RF-003` Replace one direct publisher with the generated SDK plus Dapr.
    Definition of done: one existing producer no longer hand-builds envelopes.

### Adapter migration

13. `MG-001` Replace `hookd_bridge` custom envelope logic with the generated
    command SDK.
    Definition of done: the bridge only maps external payloads into approved
    command types.
14. `MG-002` Replace `openclaw_bridge` publish logic with generated SDK calls.
    Definition of done: no local schema or routing-key invention remains.
15. `MG-003` Replace `infra_dispatcher` command publish logic with the new
    command contract.
    Definition of done: no flat legacy payload shape remains in the dispatch
    path.

### Observability and operations

16. `OP-001` Implement trace propagation policy across commands and events.
    Definition of done: a single correlation chain can be followed across the
    reference slice.
17. `OP-002` Implement replay tooling for one event stream and one command
    stream.
    Definition of done: operators can safely replay by correlation ID or time
    window.
18. `OP-003` Add dead-letter inspection and alerting.
    Definition of done: dead letters are visible, queryable, and actionable.

## Strict sequencing constraints

Some tickets can run in parallel. Some cannot.

These dependencies are hard constraints:

- `HF-001` and `HF-002` must land before `HF-003`.
- `HF-003` and `HF-004` should land before `HF-005`.
- `BB-001` should land before `BB-002` and `BB-003`.
- `HF-005`, `BB-002`, and `BB-003` should land before `RF-002`.
- `RF-002` should land before `RF-003`.
- `RF-003` should land before any broad adapter migration.

## Rules for every migration ticket

Every migration ticket must follow these rules:

- Move the publisher or consumer onto generated contracts.
- Remove bespoke envelope logic instead of wrapping it.
- Preserve correlation and causation identifiers.
- Add replay notes if the migration changes delivery semantics.
- Update Holyfields ownership and catalog metadata if a new contract is added.

## Definition of success for sprint one

Sprint one is successful only if all of these are true:

- Holyfields has base event and command contracts.
- The local Compose sandbox runs the selected platform stack.
- One service can publish and consume through Dapr and NATS.
- One generated Python SDK and one generated TypeScript SDK exist.
- One direct publisher in the current estate has been replaced.

## After sprint one

After sprint one, the program manager should reassess two questions:

- Is the Dapr operational model acceptable to the team in daily development?
- Is the Holyfields generation workflow fast enough to become the default path?

If either answer is no, fix that immediately before expanding the migration
surface.
