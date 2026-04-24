# Bloodbank v3 — Smoke Tests

Minimal, **benign**, and **optionally idempotent** end-to-end checks that
the v3 event backbone is wired correctly. Two complementary tests:

| Test | What it proves | Requires |
|---|---|---|
| `smoketest.sh` | NATS JetStream is reachable, streams exist, envelope round-trips unchanged | `nats` + `nats-init` |
| `smoketest-dapr.sh` | Dapr `bloodbank-v3-pubsub` component loads, publish HTTP API works, Dapr→NATS routing hits the expected subject | `nats` + `nats-init` + `dapr-placement` + `daprd-smoketest` |
| `smoketest-dapr-subscribe.sh` | Dapr delivers a published message back to an app callback — the full publish→subscribe loop | `nats` + `nats-init` + `dapr-placement` + `echo-sub` + `daprd-subscribe` |

## Scope — `smoketest.sh` (NATS-direct)

Talks to NATS JetStream directly via the `nats` CLI in `nats-box`. It proves:

- `BLOODBANK_V3_EVENTS` stream exists and accepts `event.smoketest.ping`.
- A canonical CloudEvents envelope round-trips unchanged.
- `correlationid` preservation works.
- A durable consumer delivers the message once, and `--ack` removes it
  from the consumer's pending set.

It does **not** prove:

- Dapr pub/sub routing.
- Holyfields SDK construction (no SDK exists yet).
- Apicurio schema validation (not wired into the publish path).

## Scope — `smoketest-dapr.sh` (Dapr sidecar)

Publishes via the Dapr HTTP API (`POST /v1.0/publish/<pubsub>/<topic>`)
through a no-app `daprd` sidecar, then reads the message back through a
NATS JetStream pull consumer. It proves:

- The `pubsub.jetstream` component manifest is metadata-correct (Dapr
  loads it without error).
- ADR-0001's topic-to-subject mapping holds: Dapr topic
  `event.dapr.smoketest.ping` lands on NATS subject of the same name,
  captured by the `BLOODBANK_V3_EVENTS` stream's `event.>` binding.
- Dapr adds `topic` and `pubsubname` envelope fields (validated to
  confirm the route was Dapr, not direct-NATS).
- `correlationid`, `id`, and data payload round-trip unchanged.

It does **not** prove:

- Subscribe path delivery to an app. That is covered by
  `smoketest-dapr-subscribe.sh`.
- Holyfields SDK envelope construction.
- Apicurio schema validation.

## Scope — `smoketest-dapr-subscribe.sh` (Dapr publish → subscribe)

The first test that proves **delivery**. Publishes via the Dapr HTTP API,
then polls the `echo-sub` test app's `/inspect/received` endpoint until
the matching event arrives (via Dapr → pubsub.jetstream consumer →
`/events/smoketest` callback).

Proves:

- The `bloodbank-v3-pubsub` component's subscribe path works end-to-end
  against `BLOODBANK_V3_EVENTS`. Dapr creates a durable consumer
  (`bloodbank-v3-pubsub` on the stream) and delivers matching events.
- The programmatic subscription contract (`GET /dapr/subscribe`,
  `POST <route>`) is wired correctly.
- Dapr-added envelope fields (`topic`, `pubsubname`) survive to the app
  callback.

### Test app: `echo-sub`

A minimal Python-stdlib-only HTTP server at `ops/v3/smoketest/echo-sub/app.py`
that implements:

| Endpoint | Purpose |
|---|---|
| `GET /dapr/subscribe` | Returns the programmatic subscription list Dapr queries at startup |
| `POST /events/smoketest` | Dapr delivers matching messages here; stored in an in-memory FIFO buffer (max 1024) |
| `GET /inspect/received` | Test hook: returns everything in the buffer as JSON |
| `POST /inspect/reset` | Test hook: clear buffer, returns count cleared |
| `GET /healthz` | Liveness probe |

It is bind-mounted into a `python:3.11-alpine` container (no build step).

## Usage

### NATS-direct smoke test

```bash
# Preconditions: bring up nats + nats-init
docker compose \
  --project-name bloodbank-v3 \
  -f bloodbank/compose/v3/docker-compose.yml \
  up -d nats nats-init

# Default: fresh UUID per run
bash bloodbank/ops/v3/smoketest/smoketest.sh

# Deterministic (CI-friendly): re-running with the same correlation id is safe
bash bloodbank/ops/v3/smoketest/smoketest.sh --correlation-id ci-$(date +%Y-%m-%d)
```

### Dapr publish-only smoke test

```bash
# Preconditions: bring up Dapr-enabled sandbox via profile
docker compose \
  --project-name bloodbank-v3 \
  --profile dapr-smoketest \
  -f bloodbank/compose/v3/docker-compose.yml \
  up -d nats nats-init dapr-placement daprd-smoketest

# Default: fresh UUID per run
bash bloodbank/ops/v3/smoketest/smoketest-dapr.sh

# Deterministic
bash bloodbank/ops/v3/smoketest/smoketest-dapr.sh --correlation-id dapr-ci-$(date +%Y-%m-%d)
```

### Dapr publish → subscribe smoke test

```bash
# Preconditions: different profile (brings echo-sub + daprd-subscribe)
docker compose \
  --project-name bloodbank-v3 \
  --profile dapr-subscribe \
  -f bloodbank/compose/v3/docker-compose.yml \
  up -d nats nats-init dapr-placement echo-sub daprd-subscribe

# Default: fresh UUID per run
bash bloodbank/ops/v3/smoketest/smoketest-dapr-subscribe.sh

# Deterministic first run
bash bloodbank/ops/v3/smoketest/smoketest-dapr-subscribe.sh --correlation-id sub-ci-$(date +%Y-%m-%d)
```

### JetStream dedup and re-runs

`BLOODBANK_V3_EVENTS` has a 2-minute `DuplicateWindow`, and Dapr sets
`Nats-Msg-Id` to the CloudEvent `id` on publish. Re-running any of the
Dapr tests with the **same `--correlation-id` within that window**
correctly hits the dedup filter — the second publish returns 204 but no
message lands on the stream, so `smoketest-dapr-subscribe.sh` will time
out on the second attempt. That is the idempotency contract working as
designed, not a flake. Use a fresh `--correlation-id` per run to prove
delivery, or wait out the dedup window.

Exit codes (both tests):

- `0` — PASS; published event was received with matching id.
- `1` — sandbox/daprd not reachable, stream missing, or receive timeout.
- `2` — event received but envelope validation failed.

## Idempotency contract

Both tests treat the `correlation_id` / `event_id` as the idempotency key:
re-running with the same `--correlation-id` publishes an event with the
same id, so a downstream deduplicating consumer sees it as one logical
event. The **consumer name** used by the test is always fresh per run
(built from `$$` + nanosecond timestamp), because reusing consumer names
across runs caused stale-delivery-state races in JetStream. The consumer
is an implementation detail of the test; it is not part of the contract.

## Preconditions

- Docker running.
- Compose sandbox reachable via `docker` (the scripts use
  `docker compose ... run --rm` to execute nats-box commands; no host-side
  `nats` CLI required).
- For the Dapr test: host port `3500` reachable (`curl` from host hits
  the daprd HTTP API).

## What it does (in order)

1. Runs `nats-init` to ensure `BLOODBANK_V3_EVENTS` exists (idempotent).
2. Constructs a canonical CloudEvents envelope from
   `canonical-event.json` by substituting a fresh or provided
   `correlation_id` and `id`.
3. Adds a short-lived pull consumer (`smoketest-<pid>-<ts>` or, with
   `--correlation-id`, a deterministic name) filtered to
   `event.smoketest.ping`.
4. Publishes the envelope to the `event.smoketest.ping` subject.
5. Fetches one message from the consumer with a 10-second timeout.
6. Validates: `specversion == "1.0"`, `type == "smoketest.ping"`, `id`
   matches what we published, `correlationid` matches.
7. Removes the consumer (clean slate).
8. Prints `PASS` and exits 0.

## Why this event is "benign"

- It writes to a dedicated subject (`event.smoketest.ping`) that no
  production consumer subscribes to.
- The payload is `{"ping": true}` — no external calls, no persistence
  writes outside the event stream itself.
- The consumer is ephemeral (created and removed each run) so it does not
  accumulate state.
- Re-running is safe: with `--correlation-id` set, the consumer name is
  deterministic, and `nats consumer add` on an existing consumer updates
  rather than duplicates.

## Header contract

The smoke test does not exercise replay headers; see `../replay/README.md`
for the `Bb-Replay*` header contract. The smoke test sends no `Bb-*`
headers and expects none.

## Future

Outstanding smoke tests not yet written:

- `smoketest-command.sh` — exercises the command envelope and
  `BLOODBANK_V3_COMMANDS` stream round-trip including `reply.*`
  correlation.
- `smoketest-holyfields.sh` — exercises envelope construction via the
  Holyfields-generated SDK once HOLYF-2 lands.
- `smoketest-dlq.sh` — exercises dead-letter behavior when a consumer
  returns non-2xx from the subscription callback enough times.
