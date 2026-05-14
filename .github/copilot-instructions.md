# Copilot instructions for Bloodbank

Bloodbank is the **event backbone** for 33GOD, not an app-layer publisher service.

## Architecture truth (must follow)
- Runtime/broker: **Dapr + NATS JetStream**.
- Event format: **CloudEvents 1.0 (JSON)**.
- Operator CLI (`cli/bb.py`) is intentionally constrained and is **not** the production publish path.
- No legacy RabbitMQ/FastAPI publisher assumptions.

## Hard rules
- Do not introduce RabbitMQ-based publish paths.
- Do not add service-to-service bypasses around the broker.
- Do not invent local envelope shapes; follow generated contracts and CloudEvents conventions.
- Keep Python services stdlib-first unless a dependency is explicitly justified.

## Change discipline
- Prefer small, focused PRs tied to a ticket.
- Include runnable verification (smoketest/doctor/task output) for behavior changes.
- Update docs when architecture or operator workflows change.

## Useful repo entry points
- `AGENTS.md` — current architecture and anti-patterns.
- `cli/README.md` and `cli/bb.py` — operator CLI scope and guardrails.
- `ops/smoketest/` — integration checks and runtime validation scripts.
- `compose/` — local sandbox topology (NATS, Dapr components, support services).
