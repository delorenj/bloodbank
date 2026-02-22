# Bloodbank — GOD Document

> **Guaranteed Organizational Document** — Developer-facing reference for Bloodbank
>
> **Last Updated**: 2026-02-22
> **Domain**: Infrastructure
> **Status**: Production
> **Owner**: Lenoon 🦎 (agent:infra)

---

## Product Overview

**Bloodbank** is the **central event bus** of the 33GOD ecosystem. Every state change, agent action, and system event flows through Bloodbank as a typed event published to a RabbitMQ topic exchange.

**Components:**
- **Bloodbank API** (`event_producers/`) — FastAPI publisher at `:8682`
- **WebSocket Relay** (`websocket-relay/`) — Real-time event broadcaster at `:8683`
- **Heartbeat System** (`heartbeat/`) — Cron-driven event scheduler
- **Consumer Template** (`consumer_template/`) — FastStream drop-in for agents
- **hookd** (separate repo, `~/code/33GOD/hookd/`) — Rust daemon bridging Claude Code hooks → Bloodbank

---

## Architecture Position

```
Producer (agent/cron/hook/API client)
    ↓ POST /events/custom
Bloodbank API (:8682)
    ↓ aio_pika publish (mandatory=True, on_return_raises=True)
RabbitMQ (bloodbank.events.v1 exchange, TOPIC, durable)
    ↓ routing_key matching
Consumers:
    ├── WS Relay (:8683) — exclusive queue, routing_key=#
    │       ↓ broadcast_event() wraps with type="event"
    │       ↓ WebSocket broadcast to all connected clients
    │       ↓ Holocene nginx proxy (/ws → relay:8683)
    │       ↓ useBloodbankStream hook (filters type==="event")
    │       ↓ EventsPanel + AgentGraph render
    ├── infra-dispatcher — routing_key=webhook.plane.#
    ├── Agent inbox queues — routing_key=agent.{name}.#
    └── theboard-meeting-trigger — specific routing keys
```

**CRITICAL**: The WS relay MUST include `"type": "event"` in broadcast messages. Without it, the Holocene frontend hook silently drops all events.

---

## Running Services

| Service | Container | Port | Purpose |
|---------|-----------|------|---------|
| Bloodbank API | `33god-bloodbank` | 8682 | Event publishing + health |
| WS Relay | `33god-bloodbank-ws-relay` | 8683 | Real-time WebSocket broadcast |
| RabbitMQ | `theboard-rabbitmq` | 5673 (AMQP) / 15673 (mgmt) | Message broker |

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

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/healthz` | GET | Health check → `{"ok": true, "service": "bloodbank"}` |
| `/events/custom` | POST | Publish any event (generic) |
| `/events/agent/thread/prompt` | POST | Agent thread prompt event |
| `/events/claude/tool_use` | POST | Claude Code tool use event |
| `/events/claude/error` | POST | Claude Code error event |

### Publish Event Example
```bash
curl -X POST http://localhost:8682/events/custom \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "agent.grolf.task.complete",
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

| File | Purpose |
|------|---------|
| `heartbeat.py` | Cron-driven schedule runner (HHMM → entries) |
| `heartbeat-schedule.json` | Master schedule config |
| `events.py` | Pydantic schemas |
| `emit-agent-status.py` | Per-minute agent status + system heartbeat emitter |

**Cron entries** (system crontab):
```
* * * * * cd ~/code/33GOD/bloodbank && .venv/bin/python heartbeat/emit-agent-status.py
```

`emit-agent-status.py` publishes 12 events per cycle: 11 agent statuses + 1 system heartbeat.

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
