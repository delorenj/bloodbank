---
title: 'BB-67 walkthrough feedback dispatch candidate'
type: feature
created: '2026-11-27'
status: in-review
route: dispatch
baseline_commit: 6685deb58c070eef1be69d4789f54bfb253160f0
context: []
---

<frozen-after-approval reason="delegated BB-67 implementation scope; ticket is authoritative">

## Intent

Accept Holocene's existing frozen explicit-Send submission as one review feedback command, dispatch it through the existing Hermes gateway, and correlate only its actual final assistant answer. This candidate covers Bloodbank source, not Holocene persistence/UI delivery or the combined real pilot.

## Boundaries & Constraints

Always preserve submission, revision, recipient, correlation and retry identity. Reuse existing invocation lifecycle and final-answer contracts. Work only in this isolated tree. Keep raw feedback/session material out of public evidence.

Never mutate Plane, the primary checkout, Holocene source, installed profiles, live journals or the gateway service. No native wake path or second invocation command. Independent spec/quality review must precede main landing; the parent arranges it after this handback.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
| --- | --- | --- | --- |
| Send | Valid frozen row | Deterministic complete envelope, unchanged row payload | Schema and cross-field validation before publication |
| Retry | Exact same row, concurrent delivery, lost PubAck/ACK, restart | Same command and stored event bytes, no captured/completed reexecution | Changed immutable fields conflict |
| Unknown execution | Restarted started row without durable capture | No automatic execution; uncertain terminal evidence | Saved feedback retained by owner |
| Routing | Missing/disabled/wrong profile | No substitute profile or accepted sibling answer | Route refusal or transient outage |
| Answer | Exact final assistant/body/native join | answered only for matching answer | Empty/silent/unrelated/cancelled/failure cannot answer |
| Publication | Durable result but failed publish or ack | Replay stored results | NAK, no model rerun |

</frozen-after-approval>

## Code Map

- `bin/bb-emit`: current generic emits regenerate id/time and cannot be retry-safe with only --command-id. Add a feedback-only deterministic builder, strict schema gate and JetStream receipt; leave generic behavior unchanged.
- `services/agent-hooks/core/validate.py`: runtime naming allowlists and schema validation. Register review.feedback.send narrowly.
- `schemas/`: actual canonical on-disk tree (the guide's bloodbank/schemas means this repo's schemas). New command extends the common envelope.
- `services/hermes-gateway/bloodbank_hermes_gateway/contract.py`: Invocation, lifecycle lineage, native final-answer facts. Preserve original feedback envelope; derive internal invocation/context only.
- `services/hermes-gateway/bloodbank_hermes_gateway/adapter.py`: opt-in dedicated feedback durable in existing multiplexed gateway, shared journal/capture/outbox. Uncaptured feedback restart must not use generic invocation's replay-execution behavior.
- `services/hermes-gateway/tests/`: reuse unchanged owning tests and add disposable probes; pinned native helpers read Git object 0408fec7a153e6c32c064acd2b8053917f1525f1.
- Read-only Holocene `review-store.ts` / `reviews.ts`: note plus ordered [id,version] digest uses compact UTF-8 JSON. Its immutable row includes createdAt; delivery fields are mutable and not wire payload.

## Tasks & Acceptance

- [x] Register schema, vocabulary and pure frozen-row adapter/status mapping.
- [x] Integrate bb-emit and one opt-in gateway consumer with existing final-answer capture.
- [x] Add disposable schema, concurrency/retry/fault, profile and reply probes.
- [x] Document exact Holocene contract and separate source/install/live gates.

Given a persisted submission, when the same explicit send retries, then all wire identities and bytes remain stable. Given a crash after possible execution but before capture, when redelivered, then the gateway refuses automatic reexecution and exposes uncertainty. Given matching durable final-answer evidence, when projected, then only the original review is answered. Given the candidate, when the unchanged gateway and schema gates run in a clean environment, then they pass without relaxing existing checks.

## Implementation Notes

- Baseline unchanged gateway: 344 passed, zero skips with PYTHONPATH unset, BLOODBANK_SCHEMAS_DIR pointing at this tree, and BB30_HERMES_RELEASE=/home/delorenj/.hermes/hermes-agent. Inherited Hermes Python 3.14 site-packages in PYTHONPATH broke the project's 3.12 rpds import in initial attempts; no source defect or gate relaxation.
- Parent has explicitly reserved independent review and combined live proof for follow-up. This spec records the already-dispatched ticket scope, not a new user approval gate.
- Review repairs: bind feedback facts to full command provenance, feedback lane and exact phase lineage; require registry eligibility independently of generic overrides; hold a command-scoped kernel lock through processing; durably close exhausted deliveries and sweep completed feedback results without relying on redelivery. `test_review_feedback_regressions.py` promotes the accepted counterexamples and real-process lock release/recovery. Existing baseline tests remain unchanged.

- Second-review repair: phase-wide lock-aware journal recovery closes abandoned started/captured, exhausted pending, and pre-/post-start rejection phases without command delivery or model replay. Store immutable pre-start closure bytes, preserve prior v5 lineage, rotate bounded scan pages past live owners, and continue publication during drain. Promoted faults cover both started PubAcks, both refusal closures, partial/lost PubAcks, crash-between-transactions, live owners, killed process and cursor starvation.

## Verification

- `env -u PYTHONPATH BLOODBANK_SCHEMAS_DIR="$PWD/schemas" BB30_HERMES_RELEASE=/home/delorenj/.hermes/hermes-agent uv run --project services/hermes-gateway --extra test pytest services/hermes-gateway/tests -q --tb=short`
- `env -u PYTHONPATH mise run smoketest:schemas`
- New feedback probes use disposable journals/broker fixtures only; they are not live proof.
