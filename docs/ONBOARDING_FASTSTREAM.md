# Onboarding: Bloodbank Consumers with FastStream

**Date:** 2026-01-11
**Status:** Active
**Updated:** ADR-0002 Phase 3 - Standardized envelope handling patterns

---

## Overview

We have migrated our event consumption architecture from a custom inheritance-based class (`EventConsumer`) to **FastStream**. FastStream is a modern, framework-agnostic library that brings the developer experience of FastAPI to event-driven systems.

---

## Why the Change?

1. **Safety:** The previous custom implementation had critical issues with shared state between subclasses.
2. **Type Safety:** FastStream integrates deeply with Pydantic for automatic payload validation.
3. **Testing:** Supports Dependency Injection (`Depends`) and simple function testing.
4. **Documentation:** Auto-generates AsyncAPI documentation (like Swagger/OpenAPI for events).
5. **Architecture:** Enforces separation of concerns per ADR-0002.

---

## The Old Way (Deprecated)

**❌ DON'T DO THIS**

```python
class MyConsumer(EventConsumer):
    queue_name = "my_service_queue"
    routing_keys = ["my.event"]

    @EventConsumer.event_handler("my.event")
    async def handle(self, envelope):
        # Legacy pattern
        ...
```

**Problems:**
- Inheritance-based (tight coupling)
- Implicit envelope handling
- Shared state bugs
- Hard to test

---

## The New Way (FastStream + EventEnvelope)

**✅ DO THIS**

Per ADR-0002, all consumers must:
1. Use FastStream `@broker.subscriber` decorator
2. Explicitly unwrap `EventEnvelope`
3. Access correlation metadata
4. Use `RabbitQueue` and `RabbitExchange` objects

### Complete Example

```python
"""
my_service/consumer.py - FastStream consumer with envelope unwrapping
"""
from typing import Dict, Any
from faststream import FastStream
from faststream.rabbit import RabbitBroker, RabbitQueue, RabbitExchange, ExchangeType

from event_producers.config import settings
from event_producers.events.base import EventEnvelope
from event_producers.events.domains.fireflies import FirefliesTranscriptReadyPayload

# Initialize broker and app
broker = RabbitBroker(settings.rabbit_url)
app = FastStream(broker)

@broker.subscriber(
    queue=RabbitQueue(
        name="services.fireflies.transcript_processor",
        routing_key="fireflies.transcript.ready",
        durable=True,
    ),
    exchange=RabbitExchange(
        name=settings.exchange_name,
        type=ExchangeType.TOPIC,
        durable=True,
    ),
)
async def handle_transcript_ready(message_dict: Dict[str, Any]):
    """
    Handle fireflies.transcript.ready events.

    Unwraps EventEnvelope, processes payload, maintains correlation tracking.
    Follows ADR-0002 explicit envelope unwrapping pattern.
    """
    # Step 1: Unwrap EventEnvelope
    envelope = EventEnvelope(**message_dict)

    # Step 2: Parse typed payload
    payload = FirefliesTranscriptReadyPayload(**envelope.payload)

    # Step 3: Access correlation metadata
    correlation_ids = envelope.correlation_ids
    source = envelope.source
    event_id = envelope.event_id

    # Step 4: Business logic
    print(f"Processing transcript: {payload.title}")
    print(f"Correlation chain: {correlation_ids}")

    # Step 5: Publish side effects with correlation (optional)
    # await publisher.publish(
    #     routing_key="fireflies.transcript.processed",
    #     body=response_envelope.model_dump(),
    #     event_id=new_event_id,
    #     parent_event_ids=[envelope.event_id],
    # )
```

---

## Running Consumers

### Development (Hot Reload)

```bash
# Navigate to your service directory
cd /home/delorenj/code/33GOD/services/my-service

# Install dependencies
uv sync

# Run with hot reload
uv run faststream run src.consumer:app --reload
```

### Production (Multi-Worker)

```bash
# Run with 4 workers for high throughput
uv run faststream run src.consumer:app --workers 4
```

### Docker

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install uv && uv sync

CMD ["uv", "run", "faststream", "run", "src.consumer:app"]
```

---

## Key Patterns

### Pattern 1: Explicit EventEnvelope Unwrapping

**Always unwrap the envelope explicitly:**

```python
async def handler(message_dict: Dict[str, Any]):
    # Unwrap envelope
    envelope = EventEnvelope(**message_dict)
    payload = SpecificPayload(**envelope.payload)

    # Access metadata
    event_id = envelope.event_id
    correlation_ids = envelope.correlation_ids
    source = envelope.source
    timestamp = envelope.timestamp
    agent_context = envelope.agent_context  # For AI-triggered events

    # Business logic
    ...
```

**Why?**
- Maintains Bloodbank's envelope schema discipline
- Enables correlation tracking across service boundaries
- Provides access to source and agent context
- Type-safe with Pydantic validation

### Pattern 2: Queue Naming Convention

**Standard:** `services.<domain>.<service_name>`

```python
# ✅ Good
queue=RabbitQueue(name="services.agent.feedback_router", ...)
queue=RabbitQueue(name="services.fireflies.transcript_processor", ...)
queue=RabbitQueue(name="services.theboard.meeting_trigger", ...)

# ❌ Bad
queue=RabbitQueue(name="my_queue", ...)
queue=RabbitQueue(name="queue123", ...)
```

### Pattern 3: Lifecycle Hooks

Use FastStream app hooks for initialization/cleanup:

```python
from event_producers.rabbit import Publisher

publisher = Publisher(enable_correlation_tracking=True)

@app.after_startup
async def startup():
    await publisher.start()
    logger.info("Service started")

@app.after_shutdown
async def shutdown():
    await publisher.close()
    logger.info("Service shutdown")
```

### Pattern 4: Publishing Side Effects with Correlation

```python
from event_producers.events.base import create_envelope, Source, TriggerType

async def handler(message_dict: Dict[str, Any]):
    envelope = EventEnvelope(**message_dict)
    payload = RequestPayload(**envelope.payload)

    # Process request
    result = await process(payload)

    # Create response envelope with correlation
    response_envelope = create_envelope(
        event_type="my.event.response",
        payload=ResponsePayload(result=result),
        source=Source(
            host="my-service",
            type=TriggerType.AGENT,
            app="my-service",
        ),
        correlation_ids=[envelope.event_id],  # Link to parent
    )

    # Publish with correlation tracking
    await publisher.publish(
        routing_key="my.event.response",
        body=response_envelope.model_dump(),
        event_id=response_envelope.event_id,
        parent_event_ids=[envelope.event_id],
    )
```

---

## Configuration

### Subscriber Configuration

```python
@broker.subscriber(
    queue=RabbitQueue(
        name="services.domain.service_name",     # Queue name (follows convention)
        routing_key="domain.event.action",       # Routing pattern
        durable=True,                            # Survive broker restart
        exclusive=False,                         # Allow multiple consumers
        auto_delete=False,                       # Don't delete when unused
    ),
    exchange=RabbitExchange(
        name="bloodbank.events.v1",             # Bloodbank exchange
        type=ExchangeType.TOPIC,                # Topic-based routing
        durable=True,                           # Survive broker restart
    ),
)
```

### Environment Variables

Required for all services:

```bash
RABBIT_URL=amqp://guest:guest@localhost:5672/
EXCHANGE_NAME=bloodbank.events.v1
```

---

## Testing

### Unit Testing Handlers

```python
import pytest
from event_producers.events.base import EventEnvelope, create_envelope, Source, TriggerType
from event_producers.events.domains.fireflies import FirefliesTranscriptReadyPayload

@pytest.mark.asyncio
async def test_handler():
    # Create test envelope
    payload = FirefliesTranscriptReadyPayload(
        id="test-123",
        title="Test Transcript",
        duration=60.0,
        # ... other fields
    )

    envelope = create_envelope(
        event_type="fireflies.transcript.ready",
        payload=payload,
        source=Source(host="test", type=TriggerType.MANUAL, app="pytest"),
    )

    # Call handler directly
    await handle_transcript_ready(envelope.model_dump())

    # Assert side effects
    # ...
```

### Integration Testing with TestRabbitBroker

```python
from faststream.rabbit import TestRabbitBroker

@pytest.mark.asyncio
async def test_handler_integration():
    async with TestRabbitBroker(broker) as test_broker:
        # Publish test message
        envelope = create_envelope(...)

        await test_broker.publish(
            envelope.model_dump(),
            queue="services.domain.service_name",
        )

        # Verify consumption
        # ...
```

---

## Migration Checklist

Migrating from `EventConsumer` to FastStream:

- [ ] Add `faststream[rabbit]>=0.5.0` to `pyproject.toml`
- [ ] Remove `aio-pika` direct dependency
- [ ] Create `broker = RabbitBroker()` and `app = FastStream(broker)`
- [ ] Replace `@EventConsumer.event_handler()` with `@broker.subscriber()`
- [ ] Use `RabbitQueue` and `RabbitExchange` objects
- [ ] Add `@app.after_startup` and `@app.after_shutdown` hooks
- [ ] Update handler signature to accept `Dict[str, Any]`
- [ ] Explicitly unwrap `EventEnvelope` in handler
- [ ] Update `__init__.py` to export `app` and `broker`
- [ ] Update README with `faststream run` command
- [ ] Run `uv sync` to install dependencies
- [ ] Test imports: `uv run python -c "from src.consumer import app, broker"`

---

## Common Pitfalls

### Pitfall 1: Forgetting RabbitQueue/RabbitExchange Objects

**❌ Wrong:**
```python
@broker.subscriber(
    queue="my_queue",
    exchange="bloodbank.events.v1",
    routing_key="my.event"  # TypeError: unexpected keyword argument
)
```

**✅ Correct:**
```python
@broker.subscriber(
    queue=RabbitQueue(name="my_queue", routing_key="my.event"),
    exchange=RabbitExchange(name="bloodbank.events.v1", type=ExchangeType.TOPIC),
)
```

### Pitfall 2: Not Unwrapping EventEnvelope

**❌ Wrong:**
```python
async def handler(payload: FirefliesTranscriptReadyPayload):
    # This breaks correlation tracking!
    print(payload.title)
```

**✅ Correct:**
```python
async def handler(message_dict: Dict[str, Any]):
    envelope = EventEnvelope(**message_dict)
    payload = FirefliesTranscriptReadyPayload(**envelope.payload)
    print(payload.title)
```

### Pitfall 3: Using @broker.on_startup

**❌ Wrong:**
```python
@broker.on_startup  # AttributeError: RabbitBroker has no attribute 'on_startup'
async def startup():
    ...
```

**✅ Correct:**
```python
@app.after_startup  # Use app, not broker
async def startup():
    ...
```

---

## Architecture References

- [ADR-0002: Agent Feedback Event Architecture](/home/delorenj/code/33GOD/bloodbank/trunk-main/docs/architecture/ADR-0002-agent-feedback-architecture.md)
- [Phase 2 Implementation Summary](/home/delorenj/code/33GOD/bloodbank/trunk-main/docs/architecture/PHASE_2_IMPLEMENTATION_SUMMARY.md)
- [Bloodbank Architecture](/home/delorenj/code/33GOD/bloodbank/trunk-main/docs/ARCHITECTURE.md)
- [Service Registry](/home/delorenj/code/33GOD/services/registry.yaml)

---

## Need Help?

- FastStream docs: https://faststream.airt.ai/
- Bloodbank issues: `/home/delorenj/code/33GOD/bloodbank/trunk-main/docs/`
- Example service: `/home/delorenj/code/33GOD/services/agent-feedback-router/`
