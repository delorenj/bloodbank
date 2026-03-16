# Bloodbank — Agent Guide

Central event bus and infrastructure for the 33GOD ecosystem.

## Tech Stack

- **Language:** Python 3.12
- **Framework:** FastAPI + Uvicorn
- **Messaging:** RabbitMQ via aio-pika
- **Config:** Pydantic Settings (BaseSettings)
- **Package Manager:** uv
- **Deployment:** Docker (multi-stage), docker-compose

## Commands (mise)

| Task | Command |
|------|---------|
| Build | `mise run build` (uv sync + Docker image) |
| Build WS Relay | `mise run build:relay` |
| Deploy | `mise run deploy` (build + restart containers) |
| Test | `mise run test` (pytest) |
| Lint | `mise run lint` (ruff) |
| Logs | `mise run logs` |
| Health Check | `mise run health` (API + RabbitMQ queues) |

## Key Files

- `event_producers/` — Event publishing logic
- `heartbeat/` — Health monitoring
- `rabbit.py` — Core publisher/subscriber abstractions
- `Dockerfile` — Multi-stage production build

## Conventions

- Async-first for all I/O operations
- Events follow `{domain}.{entity}.{action}` naming pattern
- All services connect via `BLOODBANK_URL` environment variable
- Durable queues with dead-letter handling

## Anti-Patterns

- Never bypass the event bus for direct service-to-service calls
- Never hold database sessions during async operations
- Never use synchronous I/O in event handlers
