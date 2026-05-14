# Bloodbank — Agent Guide

The event backbone of the 33GOD ecosystem. Dapr runtime over NATS JetStream,
CloudEvents envelopes, AsyncAPI contracts, Apicurio for runtime schema lookup,
EventCatalog for human discovery.

## Stack

- **Broker:** NATS JetStream (durable streams: `BLOODBANK_EVENTS`, `BLOODBANK_COMMANDS`)
- **Runtime:** Dapr (pub/sub + state + secret stores defined under `compose/components/`)
- **Wire format:** CloudEvents 1.0 (JSON)
- **Schema registry:** Apicurio (read side); Holyfields generates the SDKs (write side)
- **Discovery:** EventCatalog
- **Producers/consumers:** language-agnostic. Services in this repo are
  Python stdlib-only by design.

There is no in-process broker, no RabbitMQ, and no FastAPI publisher service in
Bloodbank itself. Production traffic flows through Dapr sidecars embedded
alongside each service, using Holyfields-generated publishers.

## Layout

| Path                | Role                                                              |
|---------------------|-------------------------------------------------------------------|
| `compose/`          | Self-hosted sandbox: NATS, Dapr placement, Apicurio, EventCatalog |
| `compose/components/` | Dapr component manifests (pub/sub, state, secret store)         |
| `compose/nats/`     | JetStream topology (`streams.json`) + init script                 |
| `cli/bb.py`         | Operator CLI (`doctor`, `trace`, `replay`, `emit`)                |
| `ops/bootstrap/`    | Pre-boot platform validation                                      |
| `ops/smoketest/`    | End-to-end smoke tests (NATS-direct, Dapr publish, subscribe, heartbeat, claude-events) |
| `ops/replay/`       | Operator-facing replay workflow                                   |
| `ops/trace/`        | Correlation/causation walkthrough                                 |
| `services/`         | Reference services that participate in the sandbox                |
| `adapters/`         | Migration scaffolds for legacy producers (blocked on Holyfields)  |

## mise tasks

| Task            | Purpose                                                  |
|-----------------|----------------------------------------------------------|
| `mise run up`           | Boot the core sandbox (NATS + nats-init)         |
| `mise run up:all`       | Boot every profile (heartbeat + claude-events + Dapr smoke) |
| `mise run down`         | Tear the sandbox down (`-v` removes volumes)     |
| `mise run doctor`       | `cli/bb.py doctor` — manifest-driven scaffold check |
| `mise run repo-health`  | `cli/bb.py repo-health` — read-only git/issue/PR/check snapshot |
| `mise run repo-health:json` | `cli/bb.py repo-health --json` — structured snapshot for scripts/tools |
| `mise run repo-health:artifact` | timestamped JSON evidence file under `_bmad_output/evidence/` |
| `mise run repo-health:cleanup` | remove generated artifacts; optional `KEEP=N` retention and `REPORT=1` JSON output |
| `mise run bootstrap`    | `ops/bootstrap/check-platform.sh` — pre-boot validator |
| `mise run smoketest`    | NATS-direct event round-trip                     |
| `mise run smoketest:command` | NATS-direct command + reply round-trip      |
| `mise run smoketest:dapr`    | Dapr publish path                           |
| `mise run smoketest:dapr-subscribe` | Dapr publish → subscribe              |
| `mise run smoketest:heartbeat`      | Heartbeat producer/consumer end-to-end |
| `mise run smoketest:claude-events`  | Claude `agent.*` event round-trip      |
| `mise run smoketest:repo-health-cleanup` | local cleanup helper checks (default/KEEP/REPORT/error paths) |
| `mise run logs`         | Tail every Bloodbank container                   |

## BMAD baseline

- This repo is BMAD-initialized with a lightweight scaffold under `_bmad/` and `_bmad_output/`.
- Quickstart: read `_bmad/README.md` for ticket execution flow, then `_bmad_output/README.md` for closeout index + verification checklist expectations.
- For ticket-first work, use `_bmad/templates/ticket-execution.md` to track scope/steps/verification per issue.
- Ticket closure hygiene (ops/process tickets): include all three references before closing:
  - issue URL
  - merged PR URL
  - `_bmad_output/issue-<id>-execution.md` artifact path
- CI-failure triage (when a PR check goes red): capture a minimal evidence loop before next action:
  - failing check run URL
  - `gh run view <run-id> --log-failed` excerpt (error signature)
  - follow-up ticket URL when the fix is out of current PR scope
- For scriptable evidence capture, use: `python3 cli/bb.py repo-health --json`.
- Keep BMAD artifacts concise and ticket-scoped; avoid process bloat.

## Conventions

- Subjects: `event.{domain}.{entity}.{action}` and `command.{agent}.{action}`.
- Envelopes are CloudEvents 1.0 with `correlationid` and `causationid` headers
  on every message. Producers MUST set both.
- Schemas are owned by Holyfields; Bloodbank never invents an envelope shape.
- Sandbox compose project name is `bloodbank`; network is `bloodbank-network`;
  container names are `bloodbank-*`.

## Anti-patterns

- No service-to-service calls that bypass the broker.
- No locally-defined envelopes; everything goes through Holyfields.
- No synchronous I/O in event handlers.
- No assumptions of a centrally-running publisher service — there isn't one.
