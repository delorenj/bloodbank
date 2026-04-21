# Bloodbank v3 replay — operator workflow

This directory documents the **operator-facing replay workflow** for the v3
platform. It is a contract document: it defines the guarantees a production
replay implementation must meet. No runtime tooling beyond the `bb_v3 replay`
stub (see `../../../cli/v3/`) exists yet.

Companion docs:

- `../../../compose/v3/nats/README.md` — stream topology and the canonical
  NATS header names used for replay.
- `../trace/README.md` — correlation, causation, and W3C trace context.
- `../../../docs/architecture/v3-holyfields-contract-work.md` — the contract
  work that must land before live replays are possible end-to-end.
- Metarepo plan: `../../../../docs/architecture/v3-implementation-plan.md`.
- ADR-0001: `../../../../docs/architecture/ADR-0001-v3-platform-pivot.md`.

## Status

**Production replay is not yet implemented.** This document is the contract
the future implementation must satisfy. If you find a tool that claims to
replay traffic against a live broker without matching the rules below, stop
and escalate.

## What is safe to replay

| Category        | Replayable? | Rationale                                                                 |
|-----------------|-------------|---------------------------------------------------------------------------|
| Events          | Yes         | Immutable CloudEvents facts on `event.>`. Consumers must be idempotent on `id`. |
| Commands        | No          | Single-delivery by design. Re-issuing a command means a new `command_id`. |
| Replies         | No          | Tied to a specific command correlation; replaying desynchronizes callers. |

Commands land on the `BLOODBANK_V3_COMMANDS` stream under `workqueue`
retention. Once acked they are removed. Replaying a command is not the
semantics we want: it would re-trigger an operational side effect that was
already performed or already refused. If the outcome of a command needs to be
re-achieved, the operator issues a **new** command with a new `command_id`
and a `causation_id` referencing the original.

Only the `BLOODBANK_V3_EVENTS` stream (`event.>` subjects) is replayable.

## Identity preservation

Replays MUST NOT mutate the CloudEvents envelope. The following fields are
copied verbatim from the original message onto the replay delivery:

- `id`
- `time`
- `correlationid`
- `causationid`
- `source`
- `type`
- `subject`
- `dataschema`
- `data`

Consumers therefore must be idempotent on `id`. If a consumer needs to
distinguish a replay from the original delivery, it reads the NATS headers
documented below — not the envelope.

## Replay metadata (NATS headers)

Replay tagging happens at the transport layer via NATS headers, never by
mutating the CloudEvents envelope. The reserved header names match
`replay_posture.metadata_headers` in `../../../compose/v3/nats/streams.json`
and `../../../compose/v3/nats/README.md`:

| Header                      | Value                     | Purpose                                                      |
|-----------------------------|---------------------------|--------------------------------------------------------------|
| `Bb-Replay`                 | `"true"`                  | Marks this delivery as a replay.                             |
| `Bb-Replay-Batch-Id`        | UUID                      | Groups all messages emitted in one replay batch.             |
| `Bb-Replay-Reason`          | Human string              | Free-form operator-supplied reason (`projection-rebuild`).   |
| `Bb-Original-Publish-Time`  | RFC3339 / ISO-8601 string | Original publish time, for ordering and audit.               |

Any additional replay telemetry must use new `Bb-Replay-*` headers; do not
redefine the four above.

## Safety rules

1. **No replay against production without explicit operator opt-in.** The
   `bb_v3 replay` CLI will require an interactive confirmation step (or an
   explicit `--confirm-production` flag) before engaging a production broker.
   Dry-run is the default.
2. **Operator must confirm target stream and time window before any play
   action.** The replay tool prompts for, and records:
   - the target stream (`BLOODBANK_V3_EVENTS` only),
   - the subject filter (e.g. `event.artifact.>`),
   - the start time and end time of the replay window,
   - a human-readable `Bb-Replay-Reason`.
3. **Dapr `bloodbank-v3-pubsub` is the only sanctioned publish path.** Replay
   tooling must not bypass the pub/sub component. Consumer wiring stays
   uniform across first-delivery and replay traffic.
4. **Idempotency is the consumer's contract.** The replay operator is
   responsible for confirming the consumer can tolerate re-delivery before
   running a replay batch.

## Dead-letter handling (deferred)

DLQ inspection and redrive are part of the replay operator surface but are
**not defined in this document**. They are deferred to a later ticket. The
documented contract in `../../../compose/v3/nats/README.md#dead-letter-posture`
applies: each durable consumer gets a paired `*_DLQ_*` stream, and redrive
tooling will sit alongside replay tooling.

Until that ticket lands, dead-letter messages stay on their primary stream
past `max_deliver` behavior; there is no curated DLQ surface yet.

## Operator workflow (forward-looking)

The shape the eventual `bb_v3 replay` CLI is expected to take. **None of the
commands below exist yet**; they are the target surface.

```text
# Dry run — no broker traffic, just a batch plan
bb_v3 replay plan \
    --stream BLOODBANK_V3_EVENTS \
    --subject 'event.artifact.>' \
    --from 2026-04-20T00:00:00Z \
    --to   2026-04-20T01:00:00Z \
    --reason projection-rebuild

# Execute against non-production broker
bb_v3 replay run --plan <plan-file> --target dev

# Execute against production, with interactive confirmation
bb_v3 replay run --plan <plan-file> --target prod --confirm-production
```

The plan artifact is the auditable record for a replay batch. Each row in the
plan maps one source message to one replay delivery.
