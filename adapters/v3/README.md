# Bloodbank v3 adapters — migration scaffolds

This directory holds **documentation-only scaffolds** for the first adapter
migration wave. Adapters bridge legacy v2 producers to the v3 platform while
the pivot is in progress.

Companion docs:

- Metarepo plan: `../../../docs/architecture/v3-implementation-plan.md`.
- ADR-0001: `../../../docs/architecture/ADR-0001-v3-platform-pivot.md`.
- Holyfields contract tracker: `../../docs/architecture/v3-holyfields-contract-work.md`.
- NATS topology: `../../compose/v3/nats/README.md`.

## Role

Adapters are **bridge components**. Each one:

- Consumes an external or legacy payload (webhook, in-process Python call,
  v2 RabbitMQ message, etc.).
- Maps that payload into a **Holyfields-generated contract** (CloudEvents
  envelope for events, command envelope for commands).
- Publishes the result via **Dapr pub/sub** (`bloodbank-v3-pubsub`) onto
  **NATS JetStream**.

Adapters are the only place where legacy shapes meet v3 contracts. They are
therefore the bottleneck — and the safety net — for the migration.

## Invariant

**Adapters MUST NOT invent local envelopes.** Every outbound v3 message must
be built from a Holyfields-generated SDK type. If a required schema does not
exist yet, the adapter is blocked on Holyfields work (see
`../../docs/architecture/v3-holyfields-contract-work.md`, HOLYF-2) — it is
not licensed to roll its own shape.

Additional shared rules:

- Preserve `correlationid` / `correlation_id`, `causationid` /
  `causation_id`, and `traceparent` whenever the inbound payload already
  carries them.
- Route outbound traffic only through Dapr `bloodbank-v3-pubsub`.
- No adapter owns catalog generation, schema registry sync, or SDK
  generation. Those live in Holyfields.
- No executable migration code in this wave — READMEs only.

## Adapters

| Directory                                | v2 source replaced                                           | Target v3 publish path                                        |
|------------------------------------------|--------------------------------------------------------------|---------------------------------------------------------------|
| [`hookd/`](hookd/README.md)              | `hookd_bridge` (legacy Bloodbank CLI / RabbitMQ publish)     | Holyfields SDK -> Dapr pub/sub `bloodbank-v3-pubsub` -> NATS. |
| [`openclaw/`](openclaw/README.md)        | `openclaw_bridge` agent hooks (legacy publisher)             | Holyfields SDK -> Dapr pub/sub `bloodbank-v3-pubsub` -> NATS. |
| [`infra_dispatcher/`](infra_dispatcher/README.md) | `event_producers/infra_dispatcher.py` (Plane webhook -> RabbitMQ) | Holyfields SDK -> Dapr pub/sub `bloodbank-v3-pubsub` -> NATS. |

Each subdirectory README states the v2 component being replaced, the v3
target publish path, and the currently unresolved unknowns that block
implementation.

## Blocked on Holyfields

Real adapter code cannot be written until Holyfields delivers:

- CloudEvents 1.0 base schema + 33GOD extensions.
- Command envelope schema.
- Service-level AsyncAPI documents that define which events / commands a
  service owns.
- Generated Python SDK (adapters live in Python).

That tracker is HOLYF-2, documented in
`../../docs/architecture/v3-holyfields-contract-work.md`.
