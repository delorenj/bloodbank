---
pipeline-status:
  - new
modified: 2026-04-12T08:04:30-04:00
---
> **Note (2026-04-19):** This document describes the current-state v2 Bloodbank. The v3 direction is tracked in the metarepo at [docs/architecture/v3-implementation-plan.md](../docs/architecture/v3-implementation-plan.md).

# Bloodbank — GOD Document

> **Guaranteed Organizational Document** — Developer-facing reference for Bloodbank
>
> **Last Updated**: 2026-04-09
> **Domain**: Infrastructure
> **Status**: Production
> **Owner**: Lenoon 🦎 (agent:infra)

---

## Product Overview

**Bloodbank** is the **central event bus** of the 33GOD ecosystem. It is the **nervous system** that transports every state change, agent action, and system event as a typed, immutable event through RabbitMQ. Bloodbank events are the **absolute lifeblood** of 33GOD—what sets it apart as the most powerful agentic pipeline.

## Status note

This document describes the current deployed v2 implementation. The approved
overhaul target lives in
`docs/architecture/bloodbank-vnext.md`, and the runtime selection rationale
lives in `docs/architecture/dapr-vs-faststream.md`.

Use the vNext docs for new architecture and migration work. Use this GOD
document when you need to understand, operate, or retire the legacy stack.

**Bloodbank's Role in the Event Flow:**

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         EVENT FLOW IN 33GOD                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   1. HOLYFIELDS          2. BLOODBANK (You Are Here)        3. CANDYSTORE   │
│      (Definition)              (Transport)                  (Persistence)   │
│         │                           │                            │          │
│         │    Schema Definitions     │                            │          │
│         │──────────────────────────→│                            │          │
│         │                           │                            │          │
│         │                           │      RabbitMQ Exchange       │          │
│         │                           │    bloodbank.events.v1       │          │
│         │                           │      (TOPIC, Durable)        │          │
│         │                           │                            │          │
│         │                           │         ↓ Routes            │          │
│         │                           │                            │          │
│         │                           │    ┌──────────────┐         │          │
│         │                           │    │  Queues      │         │          │
│         │                           │    │  • agent.*   │         │          │
│         │                           │    │  • system.*  │         │          │
│         │                           │    │  • webhook.* │         │          │
│         │                           │    └──────────────┘         │          │
│         │                           │                            │          │
│         │                           │─────────────────────────────→│          │
│         │                           │     Persist All (#)          │          │
│         │                           │                            │          │
│         │                           │──────────────────────────────┼──────────→
│         │                           │                            │          │
│         │                           │         ↓ Broadcast          │          │
│         │                           │                            │          │
│   4. HOLOCENE ←─────────────────────│     WS Relay (8683)         │          │
│      (Visibility)                   │                            │          │
│         │                           │         ↓ Route             │          │
│         │                           │                            │          │
│   5. AGENTS ←───────────────────────│   agent.{name}.inbox        │          │
│      (Action)                       │                            │          │
│         │                           │                            │          │
│   6. HEARTBEATROUTER ←──────────────│   system.heartbeat.tick     │          │
│      (Pulse)                        │     (every 60s)              │          │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Components:**
- **Bloodbank API** (`event_producers/`) — FastAPI publisher at `:8682`
- **WebSocket Relay** (`websocket-relay/`) — Real-time event broadcaster at `:8683`
- **Heartbeat System** (`heartbeat/`) — Cron-driven event scheduler (every 60s)
- **Consumer Template** (`consumer_template/`) — FastStream drop-in for agents
- **hookd** (separate repo, `~/code/33GOD/hookd/`) — Rust daemon bridging Claude Code hooks → Bloodbank (DO WE NEED THIS?!)

---

## Architecture Position

Bloodbank is the **transport layer** of the 33GOD event-driven architecture:

```
Holyfields (Definition) → Bloodbank (Transport) → Candystore (Persistence) → Holocene (Visibility) → Agents (Action)
```

### Event Flow Detail

```
Producer (agent/cron/hook/API client)
    ↓ POST /events/custom
Bloodbank API (:8682)
    ↓ aio_pika publish (mandatory=True, on_return_raises=True)
RabbitMQ (bloodbank.events.v1 exchange, TOPIC, durable)
    ↓ routing_key matching
Consumers:
    ├── Candystore ────────────────→ PostgreSQL (persists ALL events)
    │
    ├── WS Relay (:8683) ──────────→ Holocene (real-time display)
    │       ↓ broadcast_event() wraps with type="event"
    │       ↓ WebSocket broadcast to all connected clients
    │       ↓ Holocene nginx proxy (/ws → relay:8683)
    │       ↓ useBloodbankStream hook (filters type==="event")
    │       ↓ EventsPanel + AgentGraph render
    │
    ├── HeartbeatRouter ───────────→ Routes system.heartbeat.tick
    │       ↓ Injects into agent sessions via OpenClaw hooks
    │
    ├── infra-dispatcher ──────────→ webhook.plane.#
    │
    ├── Agent inbox queues ────────→ agent.{name}.# (claimed by agents)
    │
    └── theboard-meeting-trigger ──→ specific routing keys
```

**CRITICAL**: The WS relay MUST include `"type": "event"` in broadcast messages. Without it, the Holocene frontend hook silently drops all events.

**CRITICAL**: Candystore binds with `#` (wildcard) to receive ALL events for persistence. This is what enables Holocene to query historical events.

---

## Running Services

| Service       | Container                  | Port                       | Purpose                                                                                   |     |
| ------------- | -------------------------- | -------------------------- | ----------------------------------------------------------------------------------------- | --- |
| Bloodbank API | `33god-bloodbank`          | 8682                       | Event publishing + health                                                                 |     |
| WS Relay      | `33god-bloodbank-ws-relay` | 8683                       | Real-time WebSocket broadcast                                                             |     |
| RabbitMQ      | `theboard-rabbitmq`        | 5673 (AMQP) / 15673 (mgmt) | Message broker (DEFUNCT)<br>Can we decide on a more useful first example to develop with? |     |
|               |                            |                            |                                                                                           |     |

### Build & Deploy
```bash
# From ~/code/33GOD/bloodbank/
mise build          # Python deps + Docker images (bloodbank + ws-relay)
mise deploy         # Build + restart containers

# Or from root ~/code/33GOD/
mise build:infra    # Same thing
mise deploy:infra   # Same thing
```
See `~/code/33GOD/docs/OPS.md` for full ops reference.

---

## API Endpoints

**Base URL**: `http://localhost:8682`

| Endpoint                               | Method | Description                                           |
| -------------------------------------- | ------ | ----------------------------------------------------- |
| `/healthz`                             | GET    | Health check → `{"ok": true, "service": "bloodbank"}` |
| `/events/custom`                       | POST   | Publish any event (generic)                           |
| `/events/agent/thread/prompt_received` | POST   | Agent thread prompt event                             |
| `/events/claude/tool_used`             | POST   | Claude Code tool use event                            |
| `/events/claude/error_occured`         | POST   | Claude Code error event                               |

### Publish Event Example
```bash
curl -X POST http://localhost:8682/events/custom \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "agent.grolf.task.completed",
    "event_id": "$(uuidgen)",
    "payload": {"agent": "grolf", "task": "GOD doc update"},
    "source": {"host": "big-chungus", "type": "manual", "app": "curl"},
    "timestamp": "2026-02-22T10:00:00Z",
    "version": "1.0.0"
  }'
```

**Important**: `event_id` MUST be a valid UUID (hex characters only). Non-hex characters cause 500 errors.

---

## Publisher Implementation Details

**File**: `event_producers/rabbit.py`

Critical settings (learned the hard way):
- `channel(publisher_confirms=True, on_return_raises=True)` — fail fast on unroutable messages
- `publish(mandatory=True, timeout=5)` — ensure messages reach at least one queue
- Without `on_return_raises=True`, API returns 200 but queues stay empty (silent data loss)

**EventEnvelope** requires a `source` field. The `/events/custom` endpoint accepts raw dicts — events published without `source` work for RabbitMQ routing but log Pydantic validation warnings on the broadcast side.

---

## WebSocket Relay

**File**: `websocket-relay/relay.py`

- Connects to RabbitMQ, creates exclusive queue bound with routing_key `#`
- Broadcasts to all connected WebSocket clients
- **MUST** wrap messages with `{"type": "event", "routing_key": ..., "envelope": ...}`
- Welcome message: `{"type": "welcome", ...}`
- Config via env: `RABBIT_URL`, `EXCHANGE_NAME`, `ROUTING_KEY`, `WS_HOST`, `WS_PORT`

**docker-compose.yml default**: `ROUTING_KEY: ${RELAY_ROUTING_KEY:-#}` (all events)

---

## Heartbeat System

**Directory**: `heartbeat/`

The heartbeat system is the **pulse of 33GOD**—driving agent orchestration, health monitoring, and periodic tasks across the entire ecosystem.

### Heartbeat Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         HEARTBEAT FLOW                                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   EVERY 60 SECONDS:                                                          │
│                                                                              │
│   Cron (system crontab)                                                      │
│        │                                                                     │
│        ▼                                                                     │
│   emit-agent-status.py                                                       │
│        │                                                                     │
│        ├──→ system.heartbeat.tick ───────────────────┐                       │
│        │   • tick_id (UUID)                          │                       │
│        │   • sequence_number (monotonic)             │                       │
│        │   • timestamp (ISO 8601)                    │                       │
│        │                                             │                       │
│        └──→ agent.{name}.status ─────────────────────┤                       │
│            (for each registered agent)               │                       │
│                                                      ▼                       │
│                                              RabbitMQ Exchange               │
│                                           bloodbank.events.v1                │
│                                                                              │
│                                                      │                       │
│          ┌───────────────────────────────────────────┼──────────────────┐    │
│          │                                           │                  │    │
│          ▼                                           ▼                  ▼    │
│   HeartbeatRouter                            Agent Inbox            Candystore│
│        │                                      (agent.*)              (persists)│
│        │                                           │                       │    │
│        │                                           ▼                       │    │
│        │                              ┌──────────────────────┐              │    │
│        │                              │  Agent Consumer      │              │    │
│        │                              │  (FastStream)        │              │    │
│        │                              └──────────┬───────────┘              │    │
│        │                                         │                          │    │
│        │                                         ▼                          │    │
│        │                              OpenClaw Agent Hook                   │    │
│        │                                         │                          │    │
│        │                                         ▼                          │    │
│        └────────────────────────────→  Agent Session                         │    │
│                                          (Heartbeat context                  │    │
│                                           injected into session)             │    │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Files

| File                      | Purpose                                            |
| ------------------------- | -------------------------------------------------- |
| `heartbeat.py`            | Cron-driven schedule runner (HHMM → entries)       |
| `heartbeat-schedule.json` | Master schedule config                             |
| `events.py`               | Pydantic schemas                                   |
| `emit-agent-status.py`    | Per-minute agent status + system heartbeat emitter |

### Cron Configuration

```cron
# System crontab - runs every minute
* * * * * cd ~/code/33GOD/bloodbank && .venv/bin/python heartbeat/emit-agent-status.py
```

### Heartbeat Event Schema

**`system.heartbeat.tick`**
```json
{
  "event_id": "uuid",
  "event_type": "system.heartbeat.tick",
  "timestamp": "2026-02-22T18:30:00Z",
  "source": {
    "host": "big-chungus",
    "app": "bloodbank",
    "type": "system"
  },
  "payload": {
    "tick_id": "uuid",
    "sequence_number": 12345,
    "timestamp": "2026-02-22T18:30:00Z"
  },
  "routing_key": "system.heartbeat.tick"
}
```

### HeartbeatRouter

The **HeartbeatRouter** processes `system.heartbeat.tick` events and:
1. Routes to appropriate agents via `agent.{name}.inbox`
2. Tracks agent health and responsiveness
3. Triggers periodic tasks (cleanup, sync, etc.)
4. Injects heartbeat context into agent sessions via OpenClaw hooks

### Agent Heartbeat Consumption

Agents consume heartbeats via their inbox queue:

```python
# FastStream consumer example
@broker.subscriber(
    queue=RabbitQueue(
        name="agent.myagent.inbox",
        routing_key="agent.myagent.inbox",
        durable=True
    ),
    exchange=RabbitExchange(name="bloodbank.events.v1", type=ExchangeType.TOPIC)
)
async def handle_inbox(message: dict):
    if message.get("event_type") == "system.heartbeat.tick":
        # Process heartbeat
        await process_heartbeat(message["payload"])
```

### OpenClaw Hook Injection

Heartbeat context is automatically injected into agent sessions:

```python
# OpenClaw hooks receive heartbeat context
def on_heartbeat(tick_payload: dict):
    tick_id = tick_payload["tick_id"]
    sequence = tick_payload["sequence_number"]
    # Agent can check for pending tasks, run health checks, etc.
```

> Open Question ❓
> Can we “inject” into non-openclaw, zellij sessions (like ones running claude code, codex, or gemini) ?
---

## Consumer Template

**Directory**: `consumer_template/`

Standard FastStream consumer for agents to drop in:
- Queue: `agent.{name}.inbox` (durable)
- Binding: `agent.{name}.#` on `bloodbank.events.v1`
- Retry: 3 retries with exponential backoff (5s/30s/120s)
- DLQ: `agent.{name}.dlq`

Handler signature: `async def handler(routing_key: str, payload: dict, envelope: dict)`

---

## RabbitMQ Configuration

- **Exchange**: `bloodbank.events.v1` (TOPIC, durable)
- **Credentials**: Stored in `~/code/33GOD/.env` as `RABBITMQ_USER` / `RABBITMQ_PASS`
- **Host port**: 5673 (mapped from container 5672)
- **Management**: http://localhost:15673

**Password rotation protocol**: Update `.env` → restart ALL dependent containers atomically:
```bash
# Update .env, then:
cd ~/code/33GOD && docker compose restart bloodbank bloodbank-ws-relay
```

---

## hookd (Rust Daemon)

**Source**: `~/code/33GOD/hookd/`
**Purpose**: Bridges Claude Code tool-use hooks → Bloodbank events
**Transport**: Unix socket at `/run/user/{uid}/hookd.sock`
**Limitation**: Requires git context from file paths. NOT a general event producer. Drops events without file mutation context.

Start: `HOOKD_AMQP_URL="amqp://..." hookd/target/release/hookd`

> Open Question ❓
> Do we still need this??
---

## Known Issues

1. `/events/custom` doesn't inject `source` field → Pydantic warning on broadcast side (cosmetic, events still flow)
2. hookd requires git context — silently drops non-file-mutation events
3. Schema validation at publish time not enforced (Holyfields integration TODO)

---

## References

- **Domain Doc**: `~/code/33GOD/docs/domains/infrastructure/GOD.md`
- **System Doc**: `~/code/33GOD/docs/GOD.md`
- **Ops Doc**: `~/code/33GOD/docs/OPS.md`
- **Event Schemas**: `~/code/33GOD/holyfields/schemas/`
- **Consumer Template**: `bloodbank/consumer_template/README.md`
