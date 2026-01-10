# Onboarding: Bloodbank Consumers with FastStream

**Date:** 2026-01-10
**Status:** Active

## Overview

We have migrated our event consumption architecture from a custom inheritance-based class (`EventConsumer`) to **FastStream**. FastStream is a modern, framework-agnostic library that brings the developer experience of FastAPI to event-driven systems.

## Why the Change?

1.  **Safety:** The previous custom implementation had critical issues with shared state between subclasses.
2.  **Type Safety:** FastStream integrates deeply with Pydantic. It automatically validates incoming JSON payloads against your Pydantic models before your function ever runs.
3.  **Testing:** It allows us to use Dependency Injection (`Depends`) and simple function calls for testing, rather than instantiating complex consumer classes.
4.  **Documentation:** FastStream can auto-generate AsyncAPI documentation (like Swagger/OpenAPI but for events).

## Quick Start

### 1. The Old Way (Deprecated)
*Do not use this pattern anymore.*
```python
# ❌ DON'T DO THIS
class MyConsumer(EventConsumer):
    @EventConsumer.event_handler("my.event")
    async def handle(self, envelope):
        ...
```

### 2. The New Way (FastStream)
*Use this pattern for all new consumers.*

Consumers are now just simple async functions decorated with a subscriber route.

```python
# ✅ DO THIS
from event_producers.consumer import broker
from event_producers.events.domains.fireflies import FirefliesTranscriptReadyPayload

# 1. Define the handler
@broker.subscriber(
    queue="fireflies_transcript_service",
    exchange="bloodbank.events.v1",
    routing_key="fireflies.transcript.ready"
)
async def handle_transcript_ready(payload: FirefliesTranscriptReadyPayload):
    # 'payload' is already validated and parsed!
    print(f"Processing transcript: {payload.title}")
    
    # Return values are not automatically published back unless configured, 
    # use the publisher for side effects.
```

### 3. Running Consumers

You can run consumers using the `faststream` CLI, which supports hot-reloading.

```bash
# Assuming your consumer code is in services/transcript.py
faststream run services.transcript:app --reload
```

## Key Concepts

### The Broker
Located in `event_producers/consumer.py`. This manages the connection to RabbitMQ.

```python
from event_producers.consumer import broker
```

### The App
Also in `event_producers/consumer.py`. This is the ASGI-compatible application entry point.

```python
from event_producers.consumer import app
```

### Event Types
Always use the Pydantic models from `event_producers.events.domains.*` as type hints for your handler arguments. This triggers the validation logic.

## Testing

Testing is much easier now. You can test your handler as a regular function, or use the `TestBroker` context manager.

```python
import pytest
from faststream.rabbit import TestRabbitBroker
from event_producers.consumer import broker

@pytest.mark.asyncio
async def test_handler():
    async with TestRabbitBroker(broker) as br:
        await br.publish(
            {"id": "123", "title": "Test"}, 
            queue="fireflies_transcript_service"
        )
        # Assertions...
```
