# Bloodbank v3 — Canonical Smoke Test

A minimal, **benign**, and **optionally idempotent** end-to-end check that
the v3 event backbone is wired up correctly: stream exists, publisher can
send, a durable consumer can receive, and the envelope round-trips its
CloudEvents shape.

## Scope

This is the **pre-Dapr smoke test**. It talks to NATS JetStream directly
via the `nats` CLI (in `nats-box`). It proves:

- `BLOODBANK_V3_EVENTS` stream exists and accepts `event.smoketest.ping`.
- A canonical CloudEvents envelope round-trips unchanged.
- `correlationid` preservation works.
- A durable consumer delivers the message once, and `--ack` removes it
  from the consumer's pending set.

It does **not** prove:

- Dapr pub/sub routing (no sidecar wired in the scaffold wave).
- Holyfields SDK construction (no SDK exists yet).
- Apicurio schema validation (not wired into the publish path).

Those are covered by separate tests that land with the Dapr sidecar and
Holyfields SDK tickets.

## Usage

```bash
# Default: fresh UUID per run, exercises publish + receive
bash ops/v3/smoketest/smoketest.sh

# Idempotent: deterministic id (e.g. useful in CI); re-running is safe
bash ops/v3/smoketest/smoketest.sh --correlation-id smoketest-ci-$(date +%Y-%m-%d)
```

Exit codes:

- `0` — smoke test passed; published event was received with matching id.
- `1` — sandbox not reachable, stream missing, or receive timeout.
- `2` — event received but envelope validation failed.

## Preconditions

- Docker running.
- Compose sandbox reachable via `docker` (the script uses
  `docker compose ... run --rm` to execute nats-box commands; no host-side
  `nats` CLI required).

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

Once the Dapr sidecar + Holyfields SDK land:

- A second smoketest (`smoketest-dapr.sh` or equivalent) exercises the
  publish path through Dapr pub/sub.
- A third (`smoketest-command.sh`) exercises the command envelope and
  `BLOODBANK_V3_COMMANDS` stream round-trip.
