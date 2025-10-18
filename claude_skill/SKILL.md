# Claude Skill: Event Bus Publisher

This skill enables Claude to understand and interact with our home network's event bus. It provides the necessary context to create and publish events using the existing Python framework.

## Key Concepts

The event system is built on the following components:

- **RabbitMQ:** A message broker that facilitates communication between different services.
- **`aio_pika`:** A Python library for interacting with RabbitMQ.
- **Pydantic:** A library for data validation and settings management.
- **FastAPI:** A web framework used to expose endpoints for publishing events.

### Event Structure

Events are defined as Pydantic models in `event_producers/events.py`. All events use a generic `EventEnvelope[T]` that contains metadata and a typed payload.

The `EventEnvelope` structure:

```python
class EventEnvelope(BaseModel, Generic[T]):
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str  # e.g., claude.skill.task.completed
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0.0"
    source: Source
    correlation_id: Optional[uuid.UUID] = None
    agent_context: Optional[AgentContext] = None
    payload: T  # Your typed event data
```

### Publishing Events

Use the real `Publisher` class from `rabbit.py`:

```python
from event_producers.events import EventEnvelope, Source, AgentContext
from rabbit import Publisher

# Create typed envelope
envelope = EventEnvelope[YourPayloadType](
    event_type="your.event.type",
    source=Source(component="claude-skill", host_id="localhost"),
    agent_context=AgentContext(agent_instance_id="claude-session"),
    payload=your_payload_instance
)

# Publish
publisher = Publisher()
await publisher.start()
await publisher.publish("your.routing.key", envelope.model_dump(mode="json"))
await publisher.close()
```

## How to Create and Publish a New Event

1. **Define the event payload:** Create a Pydantic model for your event data
2. **Import the real classes:** Use `EventEnvelope`, `Source`, `AgentContext` from `event_producers.events`
3. **Use the real Publisher:** Import from `rabbit.py` (not a mock)
4. **Create typed envelope:** Use `EventEnvelope[YourPayloadType]` for type safety

See `example.py` for a working demonstration using the actual event system.