# Bloodbank v3 Compose sandbox

This directory holds the first self-hosted scaffold for the 33GOD v3 event
platform. It is a **sandbox**, not production wiring. No real event traffic
is published through Dapr or NATS yet; that is planned in later tickets.

Architectural source of truth:

- `docs/architecture/v3-implementation-plan.md` (metarepo)
- `docs/architecture/ADR-0001-v3-platform-pivot.md` (metarepo)

## Invariants (locked by ADR-0001)

- Compose project name: `bloodbank-v3`
- Docker network: `bloodbank-v3-network`
- Dapr pub/sub component: `bloodbank-v3-pubsub`
- Dapr app ID prefix: `bloodbank-v3-`
- NATS events stream: `BLOODBANK_V3_EVENTS`
- NATS commands stream: `BLOODBANK_V3_COMMANDS`
- NATS subject prefixes: `event.`, `command.`, `reply.`
- Env var prefix: `BLOODBANK_V3_`

## Layout

```
compose/v3/
  docker-compose.yml          # sandbox services
  README.md                   # this file
  components/                 # Dapr component manifests
    pubsub.yaml               # bloodbank-v3-pubsub (NATS JetStream)
    statestore.yaml           # bloodbank-v3-statestore (local/in-memory default)
    secretstore.yaml          # bloodbank-v3-secretstore (env, prefix BLOODBANK_V3_)
  nats/
    README.md                 # subject conventions, retention, replay posture
    streams.json              # BLOODBANK_V3_EVENTS + BLOODBANK_V3_COMMANDS definitions
  apicurio/
    README.md                 # Apicurio Registry runtime notes
  eventcatalog/
    README.md                 # EventCatalog runtime notes
```

## Services

| Service             | Image                                      | Host port(s)                        | Purpose                                         |
|---------------------|--------------------------------------------|-------------------------------------|-------------------------------------------------|
| `nats`              | `nats:2.10-alpine`                         | `4222` (client), `8222` (monitor)   | JetStream broker (`BLOODBANK_V3_EVENTS` + `_COMMANDS`) |
| `nats-init`         | `natsio/nats-box:0.14.5`                   | -                                   | Oneshot: applies `nats/streams.json` to NATS on each `up` |
| `dapr-placement`    | `daprio/dapr:1.13.0`                       | `50005`                             | Dapr actor placement for future sidecars        |
| `apicurio-registry` | `apicurio/apicurio-registry:3.0.6`         | `8080`                              | Runtime schema registry (read side)             |
| `eventcatalog`      | `quay.io/eventcatalog/eventcatalog:2.11.1` | `3000`                              | Human/agent event discovery UI                  |

All ports can be overridden by `BLOODBANK_V3_*_PORT` env vars without editing
the compose file (see the service definitions for exact names).

Dapr sidecars are intentionally **not** defined yet. When V3-005 onward adds
app services, each sidecar must mount `./components` read-only so that
`bloodbank-v3-pubsub`, `bloodbank-v3-statestore`, and `bloodbank-v3-secretstore`
are loaded.

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
  --project-name bloodbank-v3 \
  -f compose/v3/docker-compose.yml \
  up -d
```

## Tearing down

```bash
# stop + remove containers, keep named volumes
docker compose \
  --project-name bloodbank-v3 \
  -f compose/v3/docker-compose.yml \
  down

# stop + remove containers AND named volumes
docker compose \
  --project-name bloodbank-v3 \
  -f compose/v3/docker-compose.yml \
  down -v
```

## Static validation

The following does not require pulling images; it only parses the file:

```bash
docker compose \
  --project-name bloodbank-v3 \
  -f compose/v3/docker-compose.yml \
  config
```

## Dapr component manifests

Each file in `components/` is loaded by a Dapr sidecar once app services land.

- **`pubsub.yaml`** — `bloodbank-v3-pubsub`; `pubsub.jetstream` targeting
  `nats://nats:4222`, events stream `BLOODBANK_V3_EVENTS`, events subject
  scope `event.>`. Commands and replies are handled by a future command
  bus component rather than the pub/sub abstraction.
- **`statestore.yaml`** — `bloodbank-v3-statestore`; `state.in-memory` is
  the scaffold default. See "State store tradeoff" below.
- **`secretstore.yaml`** — `bloodbank-v3-secretstore`;
  `secretstores.local.env` with `prefix: BLOODBANK_V3_`. Services resolve
  secrets like `BLOODBANK_V3_NATS_TOKEN` through this component rather
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

See `nats/README.md` for subject conventions (`event.<domain>.<entity>.<action>`,
`command.<target>.<verb>`, `reply.<target>.<verb>`), retention posture, and
replay metadata header names. See `nats/streams.json` for the machine-readable
stream definitions.

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
  --project-name bloodbank-v3 \
  -f compose/v3/docker-compose.yml \
  run --rm nats-init
```

## Apicurio + EventCatalog

See `apicurio/README.md` and `eventcatalog/README.md`. Holyfields (tracked
under V3-010 → HOLYF-2) is the write side; these services are read/display
surfaces.

## Scope guard

Do not edit files outside `compose/v3/` based on this README. Adapter, CLI,
ops, and docs work live under separate V3 tickets.
