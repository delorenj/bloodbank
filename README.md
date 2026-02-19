# Home Network Event Bus

This project provides a robust, asynchronous event bus for a home network, using RabbitMQ as the message broker and FastAPI for publishing events via a simple HTTP interface.

## Quick Start

For new deployments, see **[GREENFIELD_DEPLOYMENT.md](GREENFIELD_DEPLOYMENT.md)** for a simplified setup guide with correlation tracking enabled by default.

For migrations from v1, see **[MIGRATION_v1_to_v2.md](docs/MIGRATION_v1_to_v2.md)** for phased rollout instructions.

## Architecture

The system is composed of three main parts:

1.  **RabbitMQ Broker:** A central RabbitMQ instance that routes messages. It uses a single, durable topic exchange (`bloodbank.events.v1`) which allows for flexible routing of messages based on a "routing key."
2.  **Publisher Service (`http.py`):** A FastAPI application that exposes HTTP endpoints for publishing events. It takes a JSON payload, wraps it in a standardized `EventEnvelope`, and publishes it to the RabbitMQ exchange.
3.  **Subscriber Services:** Any number of services that connect to RabbitMQ, bind a queue to the exchange with a specific routing key, and consume events asynchronously.

## How to Publish Events

Publishing an event involves three steps: defining the event payload, wrapping it in an envelope, and publishing it. The `http.py` service provides a convenient way to do this via an HTTP POST request, but you can also publish directly from any Python service.

### 1. Define Your Event Payload

All event payloads are defined as Pydantic models in `events.py`. To create a new event, add a new `BaseModel` class.

**Example:** A `CalendarEvent` has been added to `events.py` as an example:

```python
# In events.py

class CalendarEvent(BaseModel):
    summary: str
    start_time: datetime
    end_time: datetime
    location: Optional[str] = None
```

### 2. Create the Event Envelope

The `EventEnvelope` is a standardized wrapper for all events. The `envelope_for` helper function makes it easy to create one. The `event_type` is a string that will be used as the routing key in RabbitMQ (e.g., `calendar.event.created`).

**Example:**

```python
from events import CalendarEvent, envelope_for

# Create an instance of your new event payload
new_event = CalendarEvent(
    summary="Team Meeting",
    start_time="2024-10-28T10:00:00Z",
    end_time="2024-10-28T11:00:00Z",
    location="Conference Room 4"
)

# Wrap it in an envelope
envelope = envelope_for(
    event_type="calendar.event.created",
    source="my_calendar_app",
    data=new_event
)
```

### 3. Publish the Event

You can publish an event in two main ways:

#### A) Via the FastAPI Service (Recommended)

The easiest way to publish is to add a new endpoint to `http.py`. An endpoint for the `CalendarEvent` has been added as an example.

**Example:** The following has been added to `http.py`:

```python
# In http.py
from events import CalendarEvent # Import your new model

@app.post("/events/calendar")
async def publish_calendar_event(ev: CalendarEvent, request: Request):
    env = envelope_for(
        "calendar.event.created", source="http/" + request.client.host, data=ev
    )
    await publisher.publish("calendar.event.created", env.model_dump(), message_id=env.id)
    return JSONResponse(env.model_dump())

```

You can then publish by sending a POST request:

```bash
curl -X POST http://localhost:8000/events/calendar -H "Content-Type: application/json" -d '{
  "summary": "Team Meeting",
  "start_time": "2024-10-28T10:00:00Z",
  "end_time": "2024-10-28T11:00:00Z",
  "location": "Conference Room 4"
}'
```

#### B) Directly from a Python Service

You can also use the `Publisher` class directly in any service.

**Example:**

```python
import asyncio
from rabbit import Publisher
from events import CalendarEvent, envelope_for

async def main():
    publisher = Publisher()
    await publisher.start()

    new_event = CalendarEvent(...) # Create your event as above
    envelope = envelope_for(...)

    await publisher.publish(
        routing_key="calendar.event.created",
        body=envelope.model_dump(),
        message_id=envelope.id
    )

    await publisher.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## How to Subscribe to Events

Subscribing to events is done by creating a consumer service that connects to RabbitMQ, declares a queue, and binds it to the main exchange with a routing key. The `subscriber_example.py` script provides a template for this.

## Team Infra dispatcher (Plane Ready -> OpenClaw)

This repo includes an event consumer at:

- `event_producers/infra_dispatcher.py`

It listens to `webhook.plane.#` events (already published by `/event`), filters
for **Issue** events that are:

- in state `unstarted` (configurable)
- AND tagged with a ready label (`ready` by default; configurable)

Then it forwards qualifying tickets to OpenClaw `/hooks/agent` so the
orchestrator can delegate work.

Before forwarding, it runs an **M2 component check gate** (automated test/validation
command per component) and includes the result in the hook context.

### Required env

```bash
OPENCLAW_HOOK_TOKEN=<shared-hook-token>
```

### Optional env

```bash
OPENCLAW_HOOK_URL=http://127.0.0.1:18789/hooks/agent
OPENCLAW_HOOK_DELIVER=false
INFRA_READY_STATES=unstarted
INFRA_READY_LABELS=ready,automation:go
INFRA_COMPONENT_LABEL_PREFIX=comp:
INFRA_DISPATCH_STATE_PATH=.infra_dispatch_state.json
INFRA_RUN_CHECKS=true
INFRA_CHECK_TIMEOUT_SECONDS=900
# Optional JSON override for component check map:
# INFRA_COMPONENT_CHECKS_JSON={"bloodbank":{"cwd":"/home/delorenj/code/33GOD/bloodbank","command":"mise x -- uv run pytest -q tests/test_infra_dispatcher.py"}}
```

### Run

```bash
uv run python -m event_producers.infra_dispatcher
```

### Running the Example Subscriber

1.  **Open a new terminal** and navigate to the project directory.

2.  **Run the subscriber:**

    ```bash
    python subscriber_example.py
    ```

    The service will start and listen for all messages (`#`).

3.  **Publish a matching event:**

    In another terminal, use `curl` to publish an artifact event via the FastAPI service:

    ```bash
    curl -X POST http://localhost:8000/events/calendar -H "Content-Type: application/json" -d '{
      "summary": "Team Meeting",
      "start_time": "2024-10-28T10:00:00Z",
      "end_time": "2024-10-28T11:00:00Z",
      "location": "Conference Room 4"
    }'
    ```

4.  **Observe the output:**

    You will see the event payload printed in the terminal where the subscriber is running.

### Customizing Your Subscriber

To listen for different events, you only need to change the `binding_key` in `subscriber_example.py`.

*   To listen for a specific event: `binding_key = "llm.prompt"`
*   To listen for a category of events: `binding_key = "llm.*"`
*   To listen for all events: `binding_key = "#"`