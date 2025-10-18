# Claude Skill: Event Bus Publisher

This skill enables Claude to understand and interact with our home network's event bus. It provides the necessary context to create and publish events using the existing Python framework.

## Key Concepts

The event system is built on the following components:

- **RabbitMQ:** A message broker that facilitates communication between different services.
- **`aio_pika`:** A Python library for interacting with RabbitMQ.
- **Pydantic:** A library for data validation and settings management.
- **FastAPI:** A web framework used to expose endpoints for publishing events.

### Event Structure

Events are defined as Pydantic models in `event_producers/events.py`. All events are wrapped in a generic `EventEnvelope`, which contains metadata about the event, such as its `event_id`, `event_type`, `timestamp`, and `source`.

The `EventEnvelope` has the following structure:

```python
class EventEnvelope(BaseModel, Generic[T]):
    event_id: uuid.UUID
    event_type: str
    timestamp: datetime
    version: str
    source: Source
    correlation_id: Optional[uuid.UUID]
    agent_context: Optional[AgentContext]
    payload: T
```

The `payload` field contains the specific data for the event, which is also a Pydantic model.

### Publishing Events

Events are published to a RabbitMQ exchange using the `Publisher` class in `rabbit.py`. The `Publisher` class handles the connection to RabbitMQ and the publication of messages.

The `publish` method of the `Publisher` class takes the following arguments:

- `routing_key`: A string that determines how the message is routed to consumers.
- `body`: A dictionary containing the event data.
- `message_id`: An optional string to identify the message.
- `correlation_id`: An optional string to correlate messages.

## How to Create and Publish a New Event

To create and publish a new event, you need to follow these steps:

1. **Define the event payload:** Create a new Pydantic model in `event_producers/events.py` that defines the structure of the event's payload.

2. **Publish the event:** Use the `Publisher` class to publish the event to the event bus. You will need to create an instance of the `Publisher` class, create an `EventEnvelope` for your event, and then call the `publish` method.

See the `example.py` file for a practical demonstration of how to publish an event.