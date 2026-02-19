# Bloodbank Agent Consumer Template

FastStream-based drop-in consumer for 33GOD agents.

## Contract

- **Queue name**: `agent.{name}.inbox` (durable)
- **Exchange**: `bloodbank.events.v1` (TOPIC)
- **Binding key**: `agent.{name}.#`
- **DLQ**: `agent.{name}.dlq`

## Ack / Retry / DLQ Policy

- Success: auto-ack
- Failure: reject message (`AckPolicy.REJECT_ON_ERROR`)
- Retry chain: 3 retries with exponential backoff
  - `agent.{name}.retry.1` → 5s
  - `agent.{name}.retry.2` → 30s
  - `agent.{name}.retry.3` → 120s
- After retry cycle, message returns to inbox; persistent failures should be routed/observed via `agent.{name}.dlq` handler.

> Note: RabbitMQ guarantees ordering **within one queue** only. If two same-minute events must be ordered across queues/agents, offset schedule times by 1 minute.

## Files

- `runner.py` — template runner + config + example handler

## Usage

```bash
cd ~/code/33GOD/bloodbank
export AGENT_NAME=grolf
export RABBITMQ_URL="amqp://<user>:<pass>@<host>:5672/"
export BLOODBANK_EXCHANGE="bloodbank.events.v1"

uv run python consumer_template/runner.py
```

## Integrate in an Agent Repo

1. Copy `consumer_template/runner.py` into your agent repo.
2. Replace `example_handler` with your real business handler.
3. Set `AGENT_NAME` and `RABBITMQ_URL` in your environment.
4. Run as a long-lived process (systemd, Docker, PM2, etc.).

### Handler signature

```python
async def handler(routing_key: str, payload: dict, envelope: dict) -> None:
    ...
```

- `routing_key`: Rabbit routing key (e.g., `agent.grolf.heartbeat`)
- `payload`: envelope payload only
- `envelope`: full Bloodbank envelope

## Publish test message

```bash
python - <<'PY'
import asyncio
import aio_pika
import orjson
from datetime import datetime, timezone
from uuid import uuid4

async def main():
    conn = await aio_pika.connect_robust("amqp://guest:guest@localhost:5672/")
    ch = await conn.channel()
    ex = await ch.declare_exchange("bloodbank.events.v1", aio_pika.ExchangeType.TOPIC, durable=True)

    body = orjson.dumps({
        "event_id": str(uuid4()),
        "event_type": "agent.grolf.heartbeat",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
        "source": {"host": "local", "type": "manual", "app": "test"},
        "correlation_ids": [],
        "payload": {"prompt": "hello from test"},
    })

    await ex.publish(aio_pika.Message(body=body), routing_key="agent.grolf.heartbeat")
    await conn.close()

asyncio.run(main())
PY
```
