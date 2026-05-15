# Bloodbank — Smoke Tests

Minimal, **benign**, and **optionally idempotent** end-to-end checks that
the event backbone is wired correctly. All checks use Bloodbank Event
Naming Contract v1 envelopes — see `docs/event-naming.md`.

| Test                                 | What it proves                                                                                          | Requires                                                                                  |
|--------------------------------------|----------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| `smoketest-bloodbank-naming.sh`      | Stdlib-only contract verifier: §14 sequence × {claude, copilot} + negative probes against `cli/bb.py`    | python3                                                                                   |
| `smoketest.sh`                       | NATS JetStream reachable; `BLOODBANK_EVENTS` stream accepts a v1 envelope; round-trips unchanged          | `nats` + `nats-init`                                                                       |
| `smoketest-command.sh`               | `BLOODBANK_COMMANDS` handles `bloodbank.cmd.v1.>` → `bloodbank.rpy.v1.>` with correlation preservation    | `nats` + `nats-init`                                                                       |
| `smoketest-dapr.sh`                  | Dapr `bloodbank-pubsub` loads; publish HTTP API works; Dapr→NATS routing hits the v1 subject              | `nats` + `nats-init` + `dapr-placement` + `daprd-smoketest`                                |
| `smoketest-dapr-subscribe.sh`        | Dapr delivers a published v1 envelope back to an app callback (the full publish→subscribe loop)           | `nats` + `nats-init` + `dapr-placement` + `echo-sub` + `daprd-subscribe`                   |
| `smoketest-heartbeat.sh`             | `heartbeat-tick` emits `bloodbank.v1.system.heartbeat.received` and `heartbeat-recorder` records them    | `heartbeat` compose profile                                                                |
| `smoketest-claude-events.sh`         | Six v1 events (cli, conversation, tool, agent) round-trip through Dapr to `claude-events-recorder`        | `claude-events` compose profile                                                            |

## Contract-only verifier — `smoketest-bloodbank-naming.sh`

Pure stdlib-only. No Docker, no NATS. Synthesizes the §14 canonical
sequence (15 events) for both `actor.cli=claude` and `actor.cli=copilot`
and pipes each envelope through `cli/bb.py verify-envelope`, which runs
`core.validate.assert_contract`. Also runs negative-case probes that MUST
be rejected (legacy 3-token type, banned token, wrong tense, missing
actor, subject/kind marker mismatch).

```bash
mise run smoketest:bloodbank-naming
```

Use this as a CI gate before any transport-level smoke runs.

## Scope — `smoketest.sh` (NATS-direct)

Talks to NATS JetStream directly via the `nats` CLI in `nats-box`. It proves:

- `BLOODBANK_EVENTS` exists and accepts `bloodbank.evt.v1.system.heartbeat.received`.
- A canonical v1 envelope round-trips unchanged.
- `correlationid` preservation works.
- A durable consumer delivers the message once, and `--ack` removes it
  from the consumer's pending set.

It does **not** prove:

- Dapr pub/sub routing.
- Holyfields-generated SDK construction.
- Apicurio schema validation.

## Scope — `smoketest-dapr.sh` (Dapr sidecar)

Publishes via the Dapr HTTP API (`POST /v1.0/publish/<pubsub>/<topic>`)
through a no-app `daprd` sidecar, then reads the message back through a
NATS JetStream pull consumer. It proves:

- The `pubsub.jetstream` component manifest is metadata-correct.
- Topic-to-subject mapping holds: Dapr topic
  `bloodbank.evt.v1.system.heartbeat.received` lands on the NATS subject of
  the same name, captured by the `BLOODBANK_EVENTS` stream's
  `bloodbank.evt.v1.>` binding.
- Dapr adds `topic` and `pubsubname` envelope fields.
- `correlationid`, `id`, and `data` payload round-trip unchanged.

## Scope — `smoketest-dapr-subscribe.sh` (Dapr publish → subscribe)

Publishes via the Dapr HTTP API, then polls the `echo-sub` test app's
`/inspect/received` endpoint until the matching event arrives (via Dapr →
pubsub.jetstream consumer → `/events/smoketest` callback).

Proves:

- `bloodbank-pubsub` subscribe path works end-to-end against
  `BLOODBANK_EVENTS` (`bloodbank.evt.v1.>` binding).
- Programmatic subscription contract (`GET /dapr/subscribe`,
  `POST <route>`) is wired correctly.
- Dapr-added envelope fields (`topic`, `pubsubname`) survive to the app
  callback.

### Test app: `echo-sub`

A minimal Python-stdlib-only HTTP server at `ops/smoketest/echo-sub/app.py`
that implements:

| Endpoint                  | Purpose                                                                       |
|---------------------------|-------------------------------------------------------------------------------|
| `GET /dapr/subscribe`     | Returns the programmatic subscription list Dapr queries at startup             |
| `POST /events/smoketest`  | Dapr delivers matching messages here; stored in an in-memory FIFO (max 1024)   |
| `GET /inspect/received`   | Test hook: returns everything in the buffer as JSON                            |
| `POST /inspect/reset`     | Test hook: clear buffer, returns count cleared                                 |
| `GET /healthz`            | Liveness probe                                                                 |

Bind-mounted into a `python:3.11-alpine` container (no build step).

## Usage

### Contract-only (no Docker)

```bash
mise run smoketest:bloodbank-naming
```

### NATS-direct

```bash
docker compose --project-name bloodbank \
  -f bloodbank/compose/docker-compose.yml up -d nats nats-init
bash bloodbank/ops/smoketest/smoketest.sh
# deterministic:
bash bloodbank/ops/smoketest/smoketest.sh --correlation-id ci-$(date +%Y-%m-%d)
```

### Dapr publish

```bash
docker compose --project-name bloodbank --profile dapr-smoketest \
  -f bloodbank/compose/docker-compose.yml up -d nats nats-init dapr-placement daprd-smoketest
bash bloodbank/ops/smoketest/smoketest-dapr.sh
```

### Dapr publish → subscribe

```bash
docker compose --project-name bloodbank --profile dapr-subscribe \
  -f bloodbank/compose/docker-compose.yml up -d nats nats-init dapr-placement echo-sub daprd-subscribe
bash bloodbank/ops/smoketest/smoketest-dapr-subscribe.sh
```

### JetStream dedup and re-runs

`BLOODBANK_EVENTS` has a 2-minute `DuplicateWindow`, and Dapr sets
`Nats-Msg-Id` to the CloudEvent `id` on publish. Re-running any of the
Dapr tests with the **same `--correlation-id` within that window** hits
the dedup filter — the second publish returns 204 but no message lands on
the stream. Use a fresh `--correlation-id` per run, or wait out the dedup
window.

Exit codes (all transport tests):

- `0` — PASS; envelope received with matching id.
- `1` — sandbox/daprd not reachable, stream missing, or receive timeout.
- `2` — event received but envelope validation failed.

## Idempotency contract

Re-running with the same `--correlation-id` publishes an event with the
same id, so a deduplicating consumer sees it as one logical event. The
consumer name used by the test is always fresh per run (built from `$$` +
nanosecond timestamp) — reusing consumer names across runs caused
stale-delivery-state races in JetStream.

## Header contract

Smoke tests do not exercise replay headers; see `../replay/README.md` for
the `Bb-Replay*` header contract.

## Future

Outstanding smoke tests not yet written:

- `smoketest-holyfields.sh` — exercises envelope construction via the
  Holyfields-generated SDK once HOLYF-2 lands.
- `smoketest-dlq.sh` — exercises dead-letter behavior when a consumer
  returns non-2xx from the subscription callback enough times.
