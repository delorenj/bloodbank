# Bloodbank v3 — Smoke Tests

Minimal, **benign**, and **optionally idempotent** end-to-end checks that
the v3 event backbone is wired correctly. Two complementary tests:

| Test | What it proves | Requires |
|---|---|---|
| `smoketest.sh` | NATS JetStream is reachable, streams exist, envelope round-trips unchanged | `nats` + `nats-init` |
| `smoketest-dapr.sh` | Dapr `bloodbank-v3-pubsub` component loads, publish HTTP API works, Dapr→NATS routing hits the expected subject | `nats` + `nats-init` + `dapr-placement` + `daprd-smoketest` |

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

- Subscribe path through a Dapr-attached app (requires an app callback
  port; wired when services land).
- Holyfields SDK envelope construction.
- Apicurio schema validation.

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

### Dapr smoke test

```bash
# Preconditions: bring up full Dapr-enabled sandbox via profile
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

- `smoketest-dapr-subscribe.sh` — exercises the Dapr subscribe path
  (requires a no-op app with a callback port; out-of-scope for the
  sidecar-only scaffold wave).
- `smoketest-command.sh` — exercises the command envelope and
  `BLOODBANK_V3_COMMANDS` stream round-trip including `reply.*`
  correlation.
- `smoketest-holyfields.sh` — exercises envelope construction via the
  Holyfields-generated SDK once HOLYF-2 lands.
