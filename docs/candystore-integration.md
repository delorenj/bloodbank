# Candystore Integration

Candystore is the durable audit trail for Bloodbank events. It is a sibling
repository, not a duplicate Bloodbank stack:

- `~/code/33GOD/bloodbank` owns the event backbone, schemas, naming contract,
  NATS JetStream topology, Dapr component conventions, and the compose sandbox.
- `~/code/33GOD/candystore` owns the audit application: HTTP ingestion/API,
  PostgreSQL migrations, React UI, and its per-service Dapr pub/sub component.
- `33god-platform/compose.yaml` is the active runtime bridge. It runs the
  standalone Candystore triplet beside Bloodbank and mounts Candystore's own
  Dapr component.
- `bloodbank/compose/docker-compose.yml` retains a legacy `candystore` profile
  for isolated compatibility work. It is excluded from the root stack and must
  never run beside the canonical durable consumer.

## Active Runtime Path

The root-managed event flow is:

1. Bloodbank producers publish CloudEvents to NATS/Dapr. The event stream admits
   `bloodbank.evt.>` plus explicitly registered v2 subjects.
2. `BLOODBANK_EVENTS` stores those messages in NATS JetStream.
3. `candystore-daprd` mounts Candystore's
   `dapr-components:/components:ro`.
4. Dapr calls `GET http://candystore-app:3001/dapr/subscribe`.
5. Candystore declares subscription:
   `pubsubname=bloodbank-pubsub`, `topic=bloodbank.evt.>`,
   `route=/events/all`.
6. Dapr POSTs matching events to `http://candystore-app:3001/events/all`.
7. Candystore persists the full envelope in PostgreSQL and exposes query/UI
   routes on the app port.

The broad Candystore topic is a durable-history choice, not an event-admission
choice. JetStream still stores only subjects explicitly configured on
`BLOODBANK_EVENTS`.

On the host, the default ports are:

- Candystore app/API: `http://127.0.0.1:8683`
- Candystore Dapr sidecar: `http://127.0.0.1:3504`

Quick probes:

```bash
curl -fsS http://127.0.0.1:8683/dapr/subscribe
curl -fsS http://127.0.0.1:3504/v1.0/metadata
curl -fsS 'http://127.0.0.1:8683/events?limit=3'
```

## Component Ownership

There are intentionally two Dapr pub/sub manifests with the same component
name:

- `bloodbank/compose/components/pubsub.yaml` is the shared sandbox component.
  It leaves `durableName` unset so multiple sidecars with different filters do
  not collide on one JetStream durable consumer.
- `candystore/dapr-components/pubsub.yaml` is Candystore-specific. It still
  uses `metadata.name: bloodbank-pubsub` so the app subscription matches, but it
  sets `durableName: candystore-events` and `queueGroupName: candystore` because
  Candystore is the durable audit consumer.

Do not fold Candystore into Bloodbank just to simplify the compose file. The
clean boundary is: Bloodbank owns event transport and contracts; Candystore owns
durable storage and audit UX.

## Envelope Contract

Candystore is intentionally strict. Any event delivered through this path must
conform to `docs/event-naming.md` and include the canonical top-level fields
required by Bloodbank:

- `type`: the schema-approved Bloodbank type, normally
  `bloodbank.<domain>.<entity>.<action>`
- `subject`: the matching schema-approved Bloodbank subject, normally
  `bloodbank.<evt|cmd|rpy>.v1.<domain>.<entity>.<action>`
- `domain`: must match the third token of `type`
- `kind`: `event`, `command`, or `reply`
- `correlationid` and `causationid`
- `producer`, `service`, `actor`, and `data`
- `ordering_key` for events

Do not emit snake_case CloudEvents extension aliases such as `correlation_id` or
`causation_id`. Those are not the Bloodbank contract and will not satisfy
Candystore.

## Known Drift Pattern

Hermes PM runtime consumers generated from
`hermes-agent-template/runtime-scaffold/bloodbank-consumer.py` have historically
announced online/offline presence with a hand-rolled envelope that is missing
required fields such as `domain`, `subject`, and `ordering_key`, and uses
`correlation_id`/`causation_id` instead of `correlationid`/`causationid`.

When `bloodbank-daprd-candystore` replays those messages, Candystore returns
`400` and Dapr retries the JetStream message. Fix the generator/template and
then backfill existing official PM runtimes; do not patch only the generated
file unless the template is fixed too.
