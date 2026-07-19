# Bloodbank

The event backbone of the 33GOD ecosystem. Dapr over NATS JetStream, CloudEvents
on the wire, repo-local contracts under `schemas/` driving both sides.

This repository contains the self-hosted sandbox (Docker Compose), reference
services that exercise the platform, the operator CLI (`bb`), and the operator
workflows (bootstrap, smoketest, replay, trace).

## Architecture

- **Broker:** NATS JetStream. Two durable streams: `BLOODBANK_EVENTS` for
  immutable `bloodbank.evt.v1.*` traffic, `BLOODBANK_COMMANDS` for
  `bloodbank.cmd.v1.*` / `bloodbank.rpy.v1.*` round-trips.
  round-trips. Topology in [`compose/nats/streams.json`](compose/nats/streams.json).
- **Runtime:** Dapr sidecars. Pub/sub component fronts NATS via
  [`compose/components/pubsub.yaml`](compose/components/pubsub.yaml). State and
  secret stores are siblings under the same directory.
- **Wire format:** CloudEvents 1.0 (JSON), with `correlationid` and `causationid`
  on every envelope.
- **Schemas:** Canonical source-of-truth lives in `schemas/` in this repo
  (see [`docs/event-naming.md`](docs/event-naming.md) §12). Run
  `mise run validate:schemas` to verify the tree. Runtime lookup falls
  back to a sibling `holyfields/schemas/` while downstream consumers cut
  over. Re-extraction into a neutral registry repo becomes appropriate
  once two or more serious consumers live outside Bloodbank. No envelope
  is ever invented outside the schema tree.
- **Discovery:** EventCatalog. The mount point at `compose/eventcatalog/site` is
  populated from the local schema tree.
- **Durable audit trail:** Candystore lives in the sibling
  `../candystore` repository and is run by the `candystore` compose profile.
  See [`docs/candystore-integration.md`](docs/candystore-integration.md) for the
  ownership boundary and runtime wiring.

ADR-0001 in the metarepo ratifies these decisions (TBD; not yet committed).

## Quick start

```bash
# Sanity-check the scaffold without booting Docker.
mise run doctor
mise run bootstrap

# Boot the core sandbox.
mise run up

# Run the full smoke battery once everything is healthy.
mise run smoketest
mise run smoketest:command
mise run smoketest:dapr
mise run smoketest:dapr-subscribe
mise run smoketest:heartbeat
```

The sandbox compose project is `bloodbank`; everything attaches to the
`bloodbank-network` bridge and binds host ports in the `3500–3603` / `4222` /
`8080` / `3000` range. See [`compose/README.md`](compose/README.md) for details.

## Repository layout

| Path                 | Contents                                                            |
|----------------------|---------------------------------------------------------------------|
| `compose/`           | docker-compose.yml + Dapr/NATS/Apicurio/EventCatalog/Candystore wiring |
| `cli/`               | `bb` operator CLI (`doctor`, `trace`, `replay`, `emit`)             |
| `ops/bootstrap/`     | Pre-boot file-presence validator                                    |
| `ops/smoketest/`     | End-to-end round-trip tests                                         |
| `ops/replay/`        | Operator-facing replay workflow contract                            |
| `ops/trace/`         | Correlation/causation walkthrough                                   |
| `services/`          | Reference services (heartbeat producer/consumer, event-toaster, agent hooks) |
| `adapters/`          | Migration scaffolds for legacy producers (blocked on Holyfields)    |

## Operator CLI

Run any subcommand without arguments to see usage:

```bash
python3 cli/bb.py doctor    # scaffold present-and-correct
python3 cli/bb.py trace ... # walk a correlation chain (forthcoming)
python3 cli/bb.py replay ... # replay an event into the sandbox (forthcoming)
python3 cli/bb.py emit ...   # publish a handcrafted event (forthcoming)
```

`doctor` is the only subcommand with real behavior today; the others are
intentional stubs that will be filled in as the operator surfaces land.

## CI

`.github/workflows/ci.yml` runs two jobs on every PR to `main`:

1. **static-checks** — `compileall` the CLI, run `bb doctor`, validate every
   Dapr component manifest, `streams.json`, the compose config, and shellcheck
   the operator scripts.
2. **smoke-tests** — boot the sandbox layer-by-layer and run all five smoke
   tests end-to-end against real NATS + Dapr containers.

## Conventions

- Subjects: `bloodbank.evt.v1.<domain>.<entity>.<action>`,
  `bloodbank.cmd.v1.<domain>.<entity>.<action>`, and
  `bloodbank.rpy.v1.<domain>.<entity>.<action>`.
- Every envelope carries `correlationid` and `causationid`.
- Sandbox identifiers all use the `bloodbank` prefix (project, network,
  containers). No version suffix.
- Schemas live under `schemas/`. Adapters translate; they do not invent.

## Anti-patterns

- No service-to-service calls that bypass NATS.
- No locally-defined envelopes.
- No synchronous I/O in event handlers.
- No "central publisher" service — production traffic flows through Dapr
  sidecars embedded with each service.
