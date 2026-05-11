# Bloodbank adapters — migration scaffolds

This directory holds **documentation-only scaffolds** for the first adapter
migration wave. Adapters bridge legacy v2 producers to the platform while
the pivot is in progress.

Companion docs:

- ADR-0001: ADR-0001 (metarepo, TBD).
- Holyfields contract tracker: HOLYF-2 (Holyfields contract tracker).
- NATS topology: `../compose/nats/README.md`.

## Role

Adapters are **bridge components**. Each one:

- Consumes an external or legacy payload (webhook, in-process Python call,
  v2 RabbitMQ message, etc.).
- Maps that payload into a **Holyfields-generated contract** (CloudEvents
  envelope for events, command envelope for commands).
- Publishes the result via **Dapr pub/sub** (`bloodbank-pubsub`) onto
  **NATS JetStream**.

Adapters are the only place where legacy shapes meet contracts. They are
therefore the bottleneck — and the safety net — for the migration.

## Invariant

**Adapters MUST NOT invent local envelopes.** Every outbound message must
be built from a Holyfields-generated SDK type. If a required schema does not
exist yet, the adapter is blocked on Holyfields work (see
HOLYF-2 (Holyfields contract tracker), HOLYF-2) — it is
not licensed to roll its own shape.

Additional shared rules:

- Preserve `correlationid` / `correlation_id`, `causationid` /
  `causation_id`, and `traceparent` whenever the inbound payload already
  carries them.
- Route outbound traffic only through Dapr `bloodbank-pubsub`.
- No adapter owns catalog generation, schema registry sync, or SDK
  generation. Those live in Holyfields.
- No executable migration code in this wave — READMEs only.

## Adapters

| Directory                                | v2 source replaced                                           | Target publish path                                        |
|------------------------------------------|--------------------------------------------------------------|---------------------------------------------------------------|
| [`hookd/`](hookd/README.md)              | `hookd_bridge` (legacy Bloodbank CLI / RabbitMQ publish)     | Holyfields SDK -> Dapr pub/sub `bloodbank-pubsub` -> NATS. |
| [`openclaw/`](openclaw/README.md)        | `openclaw_bridge` agent hooks (legacy publisher)             | Holyfields SDK -> Dapr pub/sub `bloodbank-pubsub` -> NATS. |
| [`infra_dispatcher/`](infra_dispatcher/README.md) | `event_producers/infra_dispatcher.py` (Plane webhook -> RabbitMQ) | Holyfields SDK -> Dapr pub/sub `bloodbank-pubsub` -> NATS. |

Each subdirectory README states the v2 component being replaced, the
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
HOLYF-2 (Holyfields contract tracker).
