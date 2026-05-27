# Candystore Integration

Candystore is the durable audit trail for Bloodbank events. It is a sibling
repository, not a duplicate Bloodbank stack:

- `~/code/33GOD/bloodbank` owns the event backbone, schemas, naming contract,
  NATS JetStream topology, Dapr component conventions, and the compose sandbox.
- `~/code/33GOD/candystore` owns the audit application: HTTP ingestion/API,
  PostgreSQL migrations, React UI, and its per-service Dapr pub/sub component.
- `bloodbank/compose/docker-compose.yml` is the local runtime bridge. The
  `candystore` profile builds `../../candystore`, runs `bloodbank-candystore`,
  attaches it to `bloodbank-network`, and gives it Postgres plus a Dapr sidecar.

## Runtime Path

The local event flow is:

1. Bloodbank producers publish CloudEvents to NATS/Dapr on subjects matching
   `bloodbank.evt.v1.>`.
2. `BLOODBANK_EVENTS` stores those messages in NATS JetStream.
3. `bloodbank-daprd-candystore` mounts
   `../../candystore/dapr-components:/components:ro`.
4. Dapr calls `GET http://candystore:3001/dapr/subscribe`.
5. Candystore declares subscription:
   `pubsubname=bloodbank-pubsub`, `topic=bloodbank.evt.v1.>`,
   `route=/events/all`.
6. Dapr POSTs matching events to `http://candystore:3001/events/all`.
7. Candystore persists the full envelope in PostgreSQL and exposes query/UI
   routes on the app port.

On the host, the default ports are:

- Candystore app/API: `http://127.0.0.1:3603`
- Candystore Dapr sidecar: `http://127.0.0.1:3505`

Quick probes:

```bash
curl -fsS http://127.0.0.1:3603/dapr/subscribe
curl -fsS http://127.0.0.1:3505/v1.0/metadata
curl -fsS 'http://127.0.0.1:3603/events?limit=3'
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

- `type`: `bloodbank.v1.<domain>.<entity>.<action>`
- `subject`: `bloodbank.<evt|cmd|rpy>.v1.<domain>.<entity>.<action>`
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
