---
name: bloodbank-event-publisher
description: Detailed instructions on how to create, build, and publish events to the DeLoNET home network's agentic developer pipeline called 33GOD. It's built with RabbitMQ in a Python ecosystem with events strictly typed via Pydantic and a FastAPI backend. The event bus, types, and helpers are collectively known as Bloodbank and is one of a handful of components that make up the 33GOD agentic developer pipeline.
---

# Bloodbank Event Publishing Guide

If you are performing any action as part of the DeLoNET home network or the 33GOD agentic development pipeline, use this skill. This skill is required to understand and interact with our home network's event bus. It provides the necessary context to create and publish events to RabbitMQ using the existing Python framework.

## Key Concepts

The event system is built on the following components:

- **RabbitMQ:** A message broker that facilitates communication between different services deployed on a k8s cluster within the DeLoNET home network.
- **`aio_pika`:** A Python library for interacting with RabbitMQ.
- **Pydantic:** A library for data validation and settings management.
- **FastAPI:** A web framework used to expose endpoints for publishing events.

### Event Structure

Events are defined as Pydantic models in `event_producers/events.py`. All events use a generic `EventEnvelope[T]` that contains metadata and a typed payload.

The `EventEnvelope` structure:

```python
class TriggerType(str, Enum):
    MANUAL = "manual"
    AGENT = "agent"
    SCHEDULED = "scheduled"
    FILE_WATCH = "file_watch"
    HOOK = "HOOK"

class Source(BaseModel):
    host: str  # e.g., localhost
    type: TriggerType  # e.g., manual, agent, scheduled, file_watch, hook
    app: Optional[str] = None  # e.g., n8n, home_assistant, claude-code, gemini-cli, etc.
    meta: Optional[Dict[str, Any]] = None  # Additional details about the trigger

class CodeState(BaseModel):
    branch: Optional[str] = None  # Git branch of the agent's codebase
    working_diff: Optional[str] = None  # Git diff of the untracked changes
    branch_diff: Optional[str] = None  # Git diff of the branch changes compared to main
    repo_url: Optional[str] = None  # URL of the repository the agent is working with
    branch: Optional[str] = None  # Branch name
    last_commit_hash: Optional[str] = None  # Last commit hash

class AgentContext(BaseModel):
    type: AgentType  # Agno, ClaudeCode, Letta, AtomicAgent, Smolagent, etc.
    name: Optional[str] = None  # e.g., "Tonny, DocumentationTzar, DevOpsAgent, Bill Paxton, etc."
    system_prompt: Optional[str] = None  # The system prompt used to initialize the agent
    instance_id: Optional[str] = None  # Unique identifier for the agent's instance - created when initializing the task
    mcp_servers: Optional[List[str]] = None  # List of MCP servers the agent is connected to
    file_references: Optional[List[str]] = None  # List of file references the agent is using
    url_references: Optional[List[str]] = None  # List of URL references the agent is using
    code_state: Optional[CodeState] = None  # Current state of the code the agent is working with
    checkpoint_id: Optional[str] = None  # Checkpoint ID if the agent supports checkpoints
    meta: Optional[Dict[str, Any]] = None  # Additional context about the agent

class EventEnvelope(BaseModel, Generic[T]):
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str  # e.g., logjangler.thread.prompt, logjangler.thread.response, fireflies.transcript.upload, etc.
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0.0"
    source: Source
    correlation_id: Optional[uuid.UUID] = None
    agent_context: Optional[AgentContext] = None
    payload: T  # Your typed event data
```

### Fireflies Event Example

```python

class FirefliesTranscriptUploadPayload(EventEnvelope):
    event_type: Literal["fireflies.transcript.upload"]
    media_file: str  # URL or path to the file to be uploaded
    media_duration_seconds: int
    media_type: str  # e.g., audio/mpeg, audio/wav, video/mp4
    created_at: datetime

class FirefliesTranscriptReadyPayload(EventEnvelope):
   meeting_id: str

```

### Publishing Events

Use the `Publisher` class from `rabbit.py`:

```python
from event_producers.events import EventEnvelope, Source, AgentContext
from rabbit import Publisher

routing_key = "fireflies.transcript.upload"

# Create typed envelope
envelope = EventEnvelope[FirefliesTranscriptReadyPayload](
    event_id: uuid.uuid4(), # This will be our correlation id to match this event with the subsequent transript.ready event
    event_type: routing_key,
    source: Source(app="n8n", type=TriggerType.FILE_WATCH, host="big-chungus")
    payload=FirefliesTranscriptUploadPayload(
      media_file="/path/to/file.mp3",
      media_duration_seconds=3600,
      media_type="audio/mp3",
    )
)
```

Then, this event is triggered by the incoming fireflies invocation of our webhook, and once the transcript is ready, we publish this event: Note that we could have directly triggered the transcription workflow from fireflies, but mandating that all mutations to state go through the event bus ensures a more consistent and auditable flow of data.

```python
from event_producers.events import EventEnvelope, Source, AgentContext
from rabbit import Publisher

routing_key = "fireflies.transcript.ready"

# Create typed envelope
envelope = EventEnvelope[FirefliesTranscriptReadyPayload](
    event_type: routing_key,
    source: Source(app="fireflies", type=TriggerType.HOOK, host="fireflies.com")
    payload=FirefliesTranscriptReadyPayload(
      meeting_id="your-meeting-id"
    )
)

# Publish
publisher = Publisher()
await publisher.start()
await publisher.publish(routing_key, envelope.model_dump(mode="json"))
await publisher.close()

```

## How to Create and Publish a New Event

1. **Define (or find) the event payload:** Payloads are all defined by a Pydantic model that is stuffed in a common base envelope `EventEnvelope[T]` in `event_producers/events.py`
2. **Use the Publisher:** Import from `rabbit.py`
3. **Create typed envelope:** Use `EventEnvelope[YourPayloadType]` for type safety
