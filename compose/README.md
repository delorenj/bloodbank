# Bloodbank Compose sandbox

This directory holds the self-hosted sandbox for the 33GOD event platform. It
is local runtime wiring, not production traffic. Profiles now carry real local
event traffic through NATS JetStream and Dapr sidecars for smoke tests,
heartbeat, Claude-event capture, and Candystore audit storage.

Architectural source of truth:

- ADR-0001 (metarepo, TBD) (metarepo)

## Invariants (locked by ADR-0001)

- Compose project name: `bloodbank`
- Docker network: `bloodbank-network`
- Dapr pub/sub component: `bloodbank-pubsub`
- Dapr app ID prefix: `bloodbank-`
- NATS events stream: `BLOODBANK_EVENTS`
- NATS commands stream: `BLOODBANK_COMMANDS`
- NATS subject prefixes: `bloodbank.evt.v1.`, `bloodbank.cmd.v1.`,
  `bloodbank.rpy.v1.`
- Env var prefix: `BLOODBANK_`

## Layout

```
compose/
  docker-compose.yml          # sandbox services
  README.md                   # this file
  components/                 # Dapr component manifests
    pubsub.yaml               # bloodbank-pubsub (NATS JetStream)
    statestore.yaml           # bloodbank-statestore (local/in-memory default)
    secretstore.yaml          # bloodbank-secretstore (env, prefix BLOODBANK_)
  nats/
    README.md                 # subject conventions, retention, replay posture
    streams.json              # BLOODBANK_EVENTS + BLOODBANK_COMMANDS definitions
  apicurio/
    README.md                 # Apicurio Registry runtime notes
  eventcatalog/
    README.md                 # EventCatalog runtime notes
```

## Services

| Service             | Profile(s)        | Host port(s)                        | Purpose                                         |
|---------------------|-------------------|-------------------------------------|-------------------------------------------------|
| `nats`              | default           | `4222` (client), `8222` (monitor)   | JetStream broker (`BLOODBANK_EVENTS` + `_COMMANDS`) |
| `nats-init`         | default           | -                                   | Oneshot: applies `nats/streams.json` to NATS on each `up` |
| `dapr-placement`    | default           | `50005`                             | Dapr actor placement for sidecars               |
| `apicurio-registry` | default           | `8080`                              | Runtime schema registry (read side)             |
| `eventcatalog`      | default           | `3000`                              | Human/agent event discovery UI                  |
| `echo-sub` + sidecar | `dapr-subscribe` | `3301`, `3501`                      | Minimal Dapr subscribe smoke app                |
| `heartbeat-*`       | `heartbeat`       | `3601`, `3502`                      | Reference heartbeat producer/consumer path      |
| `postgres`          | `candystore`      | internal                            | Candystore PostgreSQL database                  |
| `candystore` + sidecar | `candystore`   | `3603`, `3505`                      | Durable Bloodbank event audit trail             |

All ports can be overridden by `BLOODBANK_*_PORT` env vars without editing
the compose file (see the service definitions for exact names).

Dapr sidecars are defined per profile. Most sidecars mount `./components`
read-only. Candystore is the exception: its sidecar mounts
`../../candystore/dapr-components` so the audit consumer gets its own durable
JetStream consumer settings while still using the shared `bloodbank-pubsub`
component name.

## Image version caveat

The versions pinned above are conservative choices compatible with the ADR
decisions. They have **not** been pulled or runtime-verified from this
scaffold; V3-011 and the first smoke test ticket will validate them. If a
pull fails, bump to the nearest maintained patch of the same minor line
(`2.10.x` for NATS, `1.13.x` for Dapr, `3.0.x` for Apicurio, `2.11.x` for
EventCatalog) and note the change in the verification log.

## Starting the sandbox

```bash
docker compose \
  --project-name bloodbank \
  -f compose/docker-compose.yml \
  up -d
```

## Tearing down

```bash
# stop + remove containers, keep named volumes
docker compose \
  --project-name bloodbank \
  -f compose/docker-compose.yml \
  down

# stop + remove containers AND named volumes
docker compose \
  --project-name bloodbank \
  -f compose/docker-compose.yml \
  down -v
```

## Static validation

The following does not require pulling images; it only parses the file:

```bash
docker compose \
  --project-name bloodbank \
  -f compose/docker-compose.yml \
  config
```

## Dapr component manifests

Each file in `components/` is loaded by the Bloodbank-owned Dapr sidecars.

- **`pubsub.yaml`** — `bloodbank-pubsub`; `pubsub.jetstream` targeting
  `nats://nats:4222`, events stream `BLOODBANK_EVENTS`, events subject
  scope `bloodbank.evt.v1.>`. Commands and replies are carried by
  `BLOODBANK_COMMANDS` on `bloodbank.cmd.v1.>` and `bloodbank.rpy.v1.>`.
- **`statestore.yaml`** — `bloodbank-statestore`; `state.in-memory` is
  the scaffold default. See "State store tradeoff" below.
- **`secretstore.yaml`** — `bloodbank-secretstore`;
  `secretstores.local.env` with `prefix: BLOODBANK_`. Services resolve
  secrets like `BLOODBANK_NATS_TOKEN` through this component rather
  than embedding literals in manifests.

## State store tradeoff

The scaffold ships `state.in-memory` rather than `state.redis` to keep the
sandbox image count low and avoid introducing a Redis dependency before
any service actually uses state. The cost is that state is non-durable —
restarting the sidecar loses it. This is acceptable for scaffold/CI but
will be swapped for `state.redis` (or another durable backend) the first
time a service needs cross-restart state. That swap is component-manifest
only; no code change is required.

## NATS topology

See `nats/README.md` for subject conventions
(`bloodbank.evt.v1.<domain>.<entity>.<action>`,
`bloodbank.cmd.v1.<domain>.<entity>.<action>`, and
`bloodbank.rpy.v1.<domain>.<entity>.<action>`), retention posture, and replay
metadata header names. See `nats/streams.json` for the machine-readable stream
definitions.

## Candystore

The `candystore` profile builds and runs the sibling `../../candystore`
repository as the durable Bloodbank event audit trail. The compose stack knows
about Candystore; Candystore itself remains a separate service repo. The
runtime boundary, per-service Dapr component rationale, and verification probes
are documented in [`../docs/candystore-integration.md`](../docs/candystore-integration.md).

### Stream initialization

`nats/streams.json` is applied to the running NATS server by the `nats-init`
oneshot service on every `docker compose up`. It runs after `nats` reports
healthy, reads the streams manifest, and issues `nats stream add` for each
entry. It is idempotent — streams that already exist are skipped.

If you hand-edit `nats/streams.json`, bounce the sandbox (`down` then `up`)
so `nats-init` re-runs. For live edits without a restart, you can exec into
`nats-box`:

```bash
docker compose \
  --project-name bloodbank \
  -f compose/docker-compose.yml \
  run --rm nats-init
```

## Apicurio + EventCatalog

See `apicurio/README.md` and `eventcatalog/README.md`. Holyfields (tracked
under V3-010 → HOLYF-2) is the write side; these services are read/display
surfaces.

## Scope guard

Do not edit files outside `compose/` based on this README. Adapter, CLI,
ops, and docs work live under separate V3 tickets.
