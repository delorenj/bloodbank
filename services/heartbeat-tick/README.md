# heartbeat-tick

Long-running service that emits `system.heartbeat.tick` events through
Dapr pub/sub on a configurable interval. Bookended by
[`../heartbeat-recorder/`](../heartbeat-recorder/) which subscribes,
records, and exposes inspection hooks for tests.

Together these two services are the **first real-world domain event**
in the v3 platform.

## Architecture

```
┌─────────────────┐   POST /v1.0/publish/...    ┌────────────────┐
│ heartbeat-tick  │ ──────────────────────────► │ daprd-heartbeat│
│  (this service) │                              │   (sidecar)    │
└─────────────────┘                              └────────────────┘
                                                         │
                                  pubsub.jetstream       │
                                                         ▼
                            ┌────────────────────────────────────┐
                            │ NATS subject event.system.heartbeat │
                            │ stream BLOODBANK_V3_EVENTS          │
                            └────────────────────────────────────┘
                                                         │
                                                 Dapr delivers
                                                         ▼
                                  ┌────────────────────────────────┐
                                  │ POST /events/heartbeat         │
                                  │ heartbeat-recorder app         │
                                  └────────────────────────────────┘
```

A single `daprd-heartbeat` sidecar serves both sides: heartbeat-tick
publishes to its HTTP API, heartbeat-recorder is wired as the
`--app-port` consumer.

## Schema

Defined in [Holyfields](../../../holyfields/schemas/system/heartbeat.tick.v1.json).
Extends `_common/cloudevent_base.v1.json`. Required `data` fields:

| field | type | purpose |
|---|---|---|
| `tick_seq` | int | Monotonic counter from this producer instance |
| `producer_id` | string | Stable per-instance identity (logs / dedup) |
| `started_at` | RFC3339 | Producer instance start time (restart detection) |
| `interval_ms` | int (≥100) | Configured tick interval (advisory) |

The service constructs the envelope as a JSON dict directly. Switching
to the Holyfields-generated Pydantic model is a follow-up once the
Holyfields installable-package story is stable inside containers.

## Configuration (env vars)

| var | default | purpose |
|---|---|---|
| `DAPR_HTTP_HOST` | `daprd-heartbeat` | Sidecar host on compose network |
| `DAPR_HTTP_PORT` | `3500` | Sidecar HTTP port |
| `DAPR_PUBSUB` | `bloodbank-v3-pubsub` | Dapr pubsub component |
| `HEARTBEAT_TOPIC` | `event.system.heartbeat.tick` | Topic = NATS subject |
| `HEARTBEAT_INTERVAL` | `5` | Tick interval (seconds) |
| `PRODUCER_ID` | `heartbeat-tick:<random>` | Stable per-instance id |
| `LOG_LEVEL` | `INFO` | Standard Python logging level |

## Running

The compose `heartbeat` profile brings up everything:

```bash
docker compose --project-name bloodbank-v3 \
  --profile heartbeat \
  -f compose/v3/docker-compose.yml \
  up -d nats nats-init dapr-placement \
        heartbeat-recorder daprd-heartbeat heartbeat-tick
```

Verify ticks landed:

```bash
curl http://127.0.0.1:3601/inspect/recorded | jq '.count, .producers'
```

Run the integration smoke test:

```bash
bash ops/v3/smoketest/smoketest-heartbeat.sh
```

## Observability hooks

heartbeat-tick logs every successful publish: `emitted tick_seq=N id=...`.
On publish failure it logs at WARNING but does not crash; consumers
detect the gap via `tick_seq` stalling.

## Why this is "real"

Unlike the smoketest scripts under `ops/v3/smoketest/` which run
ephemerally and prove transport, this is a production-shape service:

- Long-running container (not a CI-spawned ephemeral)
- Builds CloudEvents envelopes that match the canonical schema
- Handles SIGTERM with drain semantics
- Runs as non-root in the image
- Tolerates daprd boot races
- Real consumer side (heartbeat-recorder) with persistent buffer + summary

It is the pattern reference for every future v3 service.
