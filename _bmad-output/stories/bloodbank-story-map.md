---
project: Bloodbank
artifact: BMAD story map
status: ready-for-autopilot
created: 2026-05-09
---

# Bloodbank Story Map

## Mission

Make Bloodbank read and behave as one current platform: clean source tree, clean runtime topology, contract-first events and commands, component-owned builds, and verification gates that prevent drift from creeping back in.

## Epic 1 — Clean Bloodbank Surface

### BB-STORY-001 — Source tree identity reset
**Goal:** Make paths, package labels, service names, compose names, environment names, and docs present one Bloodbank identity.

**Acceptance criteria**
- User-facing files describe the current Bloodbank architecture only.
- Version-era folder names are flattened or replaced by current names.
- Compose project, network, component, stream, app-id, and env naming is coherent.
- A search gate proves forbidden historical markers are absent from source, docs, scripts, and generated workspace artifacts.

**Notes for implementation**
- Prefer rewrite/delete over explanatory banners.
- Keep only current operational guidance.

### BB-STORY-002 — Documentation rewrite around the current platform
**Goal:** Replace scattered historical docs with a short, sharp platform guide.

**Acceptance criteria**
- `README.md` gives the current architecture, quickstart, service map, and development commands.
- Runtime docs explain only the current bus, components, and local-dev topology.
- Adapter docs explain current integration paths with no archaeology.
- Every doc links forward to the HTML cockpit as the status surface.

### BB-STORY-003 — Remove obsolete transport surface
**Goal:** Delete or replace old transport implementation, helper scripts, workflows, examples, and dependency hooks.

**Acceptance criteria**
- Obsolete broker-specific code paths are removed or replaced by current bus abstractions.
- Dependency manifests contain only packages required by the current platform.
- No helper scripts, sample workflows, or setup docs target removed infrastructure.
- Tests and import scans pass after dependency cleanup.

## Epic 2 — Componentized Runtime

### BB-STORY-004 — Parent compose orchestration
**Goal:** Make the parent compose file the single local-dev orchestrator.

**Acceptance criteria**
- Parent compose builds each service from `services/<component>/Dockerfile`.
- Shared infrastructure is declared once.
- Profiles support core, observability, recorder, and smoke-test runs.
- `mise` tasks wrap build, up, down, logs, smoke, and clean commands.

### BB-STORY-005 — Component Dockerfile standard
**Goal:** Every component owns a minimal, repeatable Dockerfile and health contract.

**Acceptance criteria**
- Each service directory has a Dockerfile, README, health endpoint/command, and expected environment list.
- Base image and Python runtime are consistent unless a component proves it needs a split.
- Images build through `mise build` without direct compose incantations.
- The HTML cockpit indexes each component and its build state.

### BB-STORY-006 — Local platform bootstrap
**Goal:** One command starts a usable local Bloodbank platform.

**Acceptance criteria**
- Local bootstrap initializes stream/state/catalog resources idempotently.
- Startup order and health checks are encoded in compose, not tribal memory.
- Failure messages point to actionable fixes.
- A clean checkout can run the smoke suite after bootstrap.

## Epic 3 — Contracts, Commands, and Replay

### BB-STORY-007 — Event contract publishing path
**Goal:** Publish contract-backed events through the current bus with schema validation.

**Acceptance criteria**
- Event envelopes are generated/validated from Holyfields contracts.
- Producers fail fast on unknown event types or invalid payloads.
- Metadata includes source, correlation, causation, timestamp, and actor where available.
- Smoke tests publish and observe at least one representative event.

### BB-STORY-008 — Command/reply path
**Goal:** Support request/response workflows without muddying immutable event history.

**Acceptance criteria**
- Commands and replies have separate subjects/topics from immutable events.
- Correlation and timeout behavior are explicit.
- Command handlers can return success, rejected, failed, or timed-out outcomes.
- Smoke tests prove command dispatch and reply correlation.

### BB-STORY-009 — Replay and trace surface
**Goal:** Make event replay and trace inspection first-class developer operations.

**Acceptance criteria**
- Developers can replay a bounded range by time, type, source, or correlation id.
- Trace metadata can show causal chains across producers and consumers.
- Replay safety rules prevent accidental duplicate side effects.
- The cockpit links to replay and trace commands/docs.

## Epic 4 — Integrations and Observability

### BB-STORY-010 — OpenClaw ingress adapter
**Goal:** Convert OpenClaw hooks into clean Bloodbank events and commands.

**Acceptance criteria**
- Incoming hooks are validated and normalized.
- Agent route naming is deterministic and documented.
- Bad payloads fail with useful error details.
- Integration tests cover representative agent events.

### BB-STORY-011 — Cadence/heartbeat components
**Goal:** Make recurring internal ticks and agent cadence events reliable and observable.

**Acceptance criteria**
- Tick generation and recording are separate components.
- Health endpoints prove schedule and delivery status.
- Duplicate tick handling is idempotent.
- Drift and missed-tick conditions are visible in logs/status.

### BB-STORY-012 — Holocene/Candystore read path
**Goal:** Ensure Bloodbank activity is visible and persistable through the rest of 33GOD.

**Acceptance criteria**
- Candystore receives normalized events for durable history.
- Holocene can display recent activity and trace views.
- Schema/catalog surfaces can be browsed by humans and agents.
- The system works locally before production wiring.

## Epic 5 — Verification and Cockpit Loop

### BB-STORY-013 — Static cleanup gates
**Goal:** Add repeatable checks that keep the repo clean after the reset.

**Acceptance criteria**
- A static gate catches forbidden historical markers, obsolete package imports, and non-current docs.
- Gate runs locally through `mise` and in CI.
- Gate output tells exactly which file needs fixing.
- The gate ignores only intentional generated metadata, if any.

### BB-STORY-014 — End-to-end smoke suite
**Goal:** Prove the platform path from publish to observe to replay.

**Acceptance criteria**
- Smoke suite starts local dependencies, publishes an event, dispatches a command, receives a reply, and verifies replay/trace.
- Suite can run from a clean checkout.
- Failures preserve logs/artifacts for debugging.
- CI has a lightweight static mode and an optional full runtime mode.

### BB-STORY-015 — HTML cockpit refresh loop
**Goal:** Keep the single-page status surface generated from source artifacts.

**Acceptance criteria**
- Cockpit includes stories, component status, decisions, risks, and verification state.
- Regeneration is scripted and idempotent.
- Vault copy is refreshed for Jarad's reading pane.
- Story status updates as BMAD/autopilot runs complete work.

## Recommended autopilot order

1. BB-STORY-001
2. BB-STORY-003
3. BB-STORY-004
4. BB-STORY-013
5. BB-STORY-002
6. BB-STORY-005 through BB-STORY-012 in component order
7. BB-STORY-014
8. BB-STORY-015

## Next autopilot chunk

Start with **BB-STORY-001 + BB-STORY-003** as one clean reset chunk. Stop only if deletion would remove a file whose current replacement is unclear.
