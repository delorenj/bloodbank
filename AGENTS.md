# Bloodbank — Agent Guide

Bloodbank is the event and command backbone for the 33GOD ecosystem.

## Tech Stack

- **Language:** Python 3.12
- **API:** FastAPI + Uvicorn
- **Event/command bus:** Dapr pub/sub over NATS JetStream
- **Config:** Pydantic Settings
- **Package Manager:** uv
- **Runtime orchestration:** Docker Compose

## Commands (mise)

| Task | Command |
|------|---------|
| Build | `mise run build` |
| Build WS Relay | `mise run build:relay` |
| Deploy | `mise run deploy` |
| Test | `mise run test` |
| Lint | `mise run lint` |
| Logs | `mise run logs` |
| Health Check | `mise run health` |

## Key Files

- `event_producers/` — publishing and command/reply flows
- `services/` — service components
- `compose/` — local topology
- `ops/` — bootstrap/smoke/runtime helpers
- `_bmad-output/stories/` — canonical story map and planning artifacts
- `_bmad-output/autopilot/logs/` — canonical execution logs

## Conventions

- Async-first I/O
- Contract-first payloads and envelopes
- Explicit correlation/causation metadata
- Replay safety over convenience

## Guardrails

- Do not treat `_bmad-output/workspace.html` as source of truth
- Update markdown/workflow artifacts first, then regenerate dashboard
- Keep docs/code aligned with one current Bloodbank identity
