# Bloodbank Event Bus - Architecture Documentation

## Executive Summary

**Bloodbank** is an event-driven message bus for the 33GOD ecosystem, built on RabbitMQ and designed to facilitate asynchronous communication between distributed services. It implements a sophisticated event/command pattern with correlation tracking, type-safe payloads, and a registry-based discovery system.

**Project Location:** `/home/delorenj/code/bloodbank/trunk-main`
**Version:** 0.2.0
**Current Branch:** feat/g3/registry
**Python Version:** 3.11+
**Key Technologies:** RabbitMQ, Redis, FastAPI, Pydantic, Textual

---

## 1. System Architecture

### 1.1 High-Level Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                      External Triggers                          │
│  (HTTP Clients, CLI, n8n Workflows, File Watchers, Webhooks)   │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FastAPI HTTP Gateway                          │
│              (event_producers/http.py)                           │
│  • Type-safe endpoint routing                                    │
│  • Event envelope creation                                       │
│  • Generic publish endpoint                                      │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Publisher Layer                              │
│                   (rabbit.py)                                    │
│  • RabbitMQ connection management                                │
│  • Optional correlation tracking via Redis                       │
│  • Deterministic event ID generation                             │
│  • Automatic retry and connection pooling                        │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                  RabbitMQ Exchange (Topic)                       │
│           Exchange: bloodbank.events.v1                          │
│  • Topic-based routing (domain.entity.action)                    │
│  • Durable, persistent messages                                  │
│  • Publisher confirms enabled                                    │
└──────────────────────┬──────────────────────────────────────────┘
                       │
         ┌─────────────┴─────────────┬──────────────────┐
         ▼                           ▼                  ▼
┌──────────────────┐    ┌──────────────────┐   ┌──────────────────┐
│ Command          │    │ Fact/Event       │   │ Consumer         │
│ Processor        │    │ Consumers        │   │ Services         │
│                  │    │                  │   │                  │
│ • Executes       │    │ • React to       │   │ • Custom         │
│   Invokable      │    │   domain events  │   │   processing     │
│   commands       │    │ • Logging        │   │ • n8n webhooks   │
│ • Publishes      │    │ • Notifications  │   │ • Analytics      │
│   side effects   │    │ • Indexing       │   │ • Integration    │
└──────────────────┘    └──────────────────┘   └──────────────────┘
```

### 1.2 Data Flow

```
HTTP Request → Envelope Creation → Publish to RabbitMQ
                                          │
                                          ├→ Redis (Correlation Tracking)
                                          │
                                          └→ Exchange Routes to Queues
                                                     │
                                                     ├→ Command Processor
                                                     │  (Invokable Commands)
                                                     │
                                                     └→ Event Consumers
                                                        (Fact Handlers)
```

---

## 2. Core Components

### 2.1 Event Envelope System

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/events/`

#### Event Envelope Structure

Every event in the system is wrapped in a standardized `EventEnvelope[T]` that provides:

```python
class EventEnvelope(BaseModel, Generic[T]):
    event_id: UUID              # Unique identifier (can be deterministic)
    event_type: str             # Routing key (e.g., "fireflies.transcript.ready")
    timestamp: datetime         # UTC timestamp
    version: str                # Envelope schema version
    source: Source              # Origin metadata
    correlation_ids: List[UUID] # Parent event IDs for causation tracking
    agent_context: Optional[AgentContext]  # AI agent metadata
    payload: T                  # Typed event data
```

**Key Files:**
- `/home/delorenj/code/bloodbank/trunk-main/event_producers/events/base.py` - Core envelope types
- `/home/delorenj/code/bloodbank/trunk-main/event_producers/events/envelope.py` - Envelope creation helpers

#### Source Metadata

Tracks WHO or WHAT triggered the event:

```python
class Source(BaseModel):
    host: str                    # Machine that generated event
    type: TriggerType            # manual|agent|scheduled|file_watch|hook
    app: Optional[str]           # Application name
    meta: Optional[Dict[str, Any]]  # Additional context
```

#### Agent Context (for AI-triggered events)

```python
class AgentContext(BaseModel):
    type: AgentType              # claude-code|gemini|letta|agno|custom
    name: Optional[str]          # Agent's persona/name
    system_prompt: Optional[str] # Initial system prompt
    instance_id: Optional[str]   # Unique session identifier
    mcp_servers: Optional[List[str]]      # Connected MCP servers
    file_references: Optional[List[str]]  # Files in context
    url_references: Optional[List[str]]   # URLs in context
    code_state: Optional[CodeState]       # Git state snapshot
    checkpoint_id: Optional[str]          # For checkpoint-based agents
```

### 2.2 Event Registry System

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/events/registry.py`

The Event Registry provides centralized type discovery and validation through a domain-based organization:

```python
registry = get_registry()
registry.auto_discover_domains()

# Type-safe payload retrieval
payload_type = registry.get_payload_type("fireflies.transcript.ready")
# Returns: FirefliesTranscriptReadyPayload class

# Domain listing
domains = registry.list_domains()
# Returns: ['agent/thread', 'fireflies', 'github', 'artifact']

# Event introspection
events = registry.list_domain_events("fireflies")
schema = registry.get_schema("fireflies.transcript.ready")
```

**Auto-Discovery Mechanism:**
1. Scans `event_producers/events/domains/` recursively
2. Imports modules containing `ROUTING_KEYS` dictionaries
3. Finds Pydantic `BaseModel` classes referenced in `ROUTING_KEYS`
4. Registers mapping: routing_key → payload_class
5. Supports nested domains (e.g., `agent/thread`)

**Domain Structure Example:**

```
event_producers/events/domains/
├── fireflies.py              # fireflies.* events
├── github.py                 # github.* events
├── artifact.py               # artifact.* events
├── llm.py                    # llm.* events
└── agent/
    └── thread.py             # agent.thread.* events
```

Each domain module defines:
```python
# Payload classes
class FirefliesTranscriptReadyPayload(BaseModel):
    id: str
    title: str
    ...

# Routing key mapping
ROUTING_KEYS = {
    "FirefliesTranscriptReadyPayload": "fireflies.transcript.ready",
}
```

### 2.3 Publisher Layer

**Location:** `/home/delorenj/code/bloodbank/trunk-main/rabbit.py`

The `Publisher` class manages RabbitMQ connections with optional Redis-backed correlation tracking:

```python
publisher = Publisher(enable_correlation_tracking=True)
await publisher.start()

# Simple publish
await publisher.publish(
    routing_key="fireflies.transcript.ready",
    body=envelope.model_dump()
)

# Publish with correlation tracking
event_id = publisher.generate_event_id(
    "fireflies.transcript.upload",
    meeting_id="abc123"
)

await publisher.publish(
    routing_key="fireflies.transcript.processed",
    body=envelope.model_dump(),
    event_id=child_event_id,
    parent_event_ids=[parent_event_id]
)
```

**Features:**
- Automatic connection recovery (`aio_pika.connect_robust`)
- Publisher confirms for reliability
- Optional correlation tracking (gracefully degrades if Redis unavailable)
- Deterministic event ID generation for idempotency
- Thread-safe with asyncio locks

### 2.4 Correlation Tracking

**Location:** `/home/delorenj/code/bloodbank/trunk-main/correlation_tracker.py`

Redis-backed system for tracking event causation chains:

```python
tracker = CorrelationTracker()
await tracker.start()

# Generate deterministic event ID
event_id = tracker.generate_event_id(
    event_type="fireflies.transcript.upload",
    unique_key="meeting_abc123"
)

# Track parent-child relationship
await tracker.add_correlation(
    child_event_id=new_event_id,
    parent_event_ids=[original_event_id],
    metadata={"reason": "transcript processed"}
)

# Query correlation chain
ancestors = await tracker.get_correlation_chain(event_id, "ancestors")
descendants = await tracker.get_correlation_chain(event_id, "descendants")

# Debug correlation data
debug_info = await tracker.debug_dump(event_id)
```

**Redis Key Structure:**
```
bloodbank:correlation:forward:{child_id}  → {parent_ids, timestamp, metadata}
bloodbank:correlation:reverse:{parent_id} → Set[child_ids]
```

**Features:**
- Deterministic UUID v5 generation (idempotency)
- Parent-child relationship tracking
- Bidirectional graph traversal (ancestors/descendants)
- 30-day TTL (configurable)
- Graceful degradation if Redis unavailable

### 2.5 Command/Event Pattern

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/events/core/`

Bloodbank implements a sophisticated Command-Query Separation (CQS) pattern:

#### Commands (Invokable Mutations)

```python
class AgentThreadPrompt(BaseCommand[AgentThreadResponse]):
    provider: str
    model: Optional[str]
    prompt: str

    async def execute(
        self,
        context: CommandContext,
        collector: EventCollector
    ) -> AgentThreadResponse:
        # Execute business logic
        response = await call_llm(self.prompt)

        # Create side effect event
        response_event = create_envelope(
            event_type="agent.thread.response",
            payload=response,
            source=context.source,
            correlation_ids=[context.correlation_id]
        )

        # Collect side effect (will be published after commit)
        collector.add(response_event)

        return response
```

**Key Interfaces:**

```python
class Invokable(ABC, Generic[R]):
    @abstractmethod
    def execute(self, context: CommandContext, collector: EventCollector) -> R:
        pass

    def rollback(self, context: CommandContext) -> None:
        pass  # Optional compensation logic

class EventCollector:
    def add(self, event: EventEnvelope) -> None:
        """Add side-effect event to publish after commit"""

    def collect(self) -> List[EventEnvelope]:
        """Retrieve and clear collected events"""
```

#### Command Manager (Transaction Orchestrator)

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/events/core/manager.py`

```python
class CommandManager:
    async def handle_envelope(self, envelope: EventEnvelope) -> None:
        # 1. Re-hydrate payload from registry
        payload_class = self.registry.get_payload_type(envelope.event_type)
        payload_obj = payload_class(**envelope.payload)

        # 2. Check if it's an Invokable command
        if isinstance(payload_obj, Invokable):
            await self._execute_command(payload_obj, envelope)

    async def _execute_command(self, command: Invokable, envelope: EventEnvelope):
        context = CommandContext(
            correlation_id=envelope.event_id,
            source_app=envelope.source.app,
            agent_context=envelope.agent_context,
            timestamp=envelope.timestamp
        )

        collector = EventCollector()

        try:
            # Execute command
            result = await command.execute(context, collector)

            # Publish side effects
            side_effects = collector.collect()
            for event in side_effects:
                await self.publisher.publish(
                    routing_key=event.event_type,
                    body=event.model_dump(),
                    event_id=event.event_id,
                    parent_event_ids=[context.correlation_id]
                )
        except Exception as e:
            command.rollback(context)
            raise
```

#### Command Processor Service

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/command_processor.py`

Standalone service that:
1. Auto-discovers all `Invokable` events from the registry
2. Subscribes to their routing keys
3. Executes commands and publishes side effects

```python
# Auto-discover command events
command_keys = []
for domain_name in registry.list_domains():
    for event_type in registry.list_domain_events(domain_name):
        payload_class = registry.get_payload_type(event_type)
        if issubclass(payload_class, Invokable):
            command_keys.append(event_type)

# Start consumer
await consumer.start(
    callback=process_message,
    routing_keys=command_keys
)
```

### 2.6 HTTP Gateway

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/http.py`

FastAPI application providing HTTP endpoints for event publishing:

```python
app = FastAPI(title="bloodbank", version="0.2.0")
publisher = Publisher(enable_correlation_tracking=True)

@app.post("/events/custom")
async def publish_custom_event(envelope: dict):
    """Generic endpoint for any event type"""
    await publisher.publish(
        routing_key=envelope["event_type"],
        body=envelope
    )

@app.post("/events/agent/thread/prompt")
async def publish_prompt(ev: AgentThreadPrompt, request: Request):
    """Type-safe endpoint for specific event"""
    source = Source(
        host=request.client.host,
        type=TriggerType.MANUAL,
        app="http-client"
    )
    envelope = await publish_event_object(ev, source)
    return envelope.model_dump()

async def publish_event_object(event: BaseEvent, source: Source):
    """OOP-style publish - auto-discovers routing key from registry"""
    routing_key = registry.reverse_lookup(type(event))
    envelope = create_envelope(event_type=routing_key, payload=event, source=source)
    await publisher.publish(routing_key, envelope.model_dump())
    return envelope
```

**Endpoints:**
- `GET /healthz` - Health check
- `POST /events/custom` - Generic event publish
- `POST /events/agent/thread/prompt` - Type-safe prompt publishing
- (Additional endpoints for each domain event type)

### 2.7 Consumer Layer

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/consumer.py`

The system uses **FastStream** for robust, type-safe event consumption:

```python
from event_producers.consumer import broker
from event_producers.events.domains.fireflies import FirefliesTranscriptReadyPayload

@broker.subscriber("fireflies_service_queue", exchange="bloodbank.events.v1", routing_key="fireflies.transcript.ready")
async def handle_transcript_ready(payload: FirefliesTranscriptReadyPayload):
    # Payload is automatically validated and typed
    print(f"Transcript ready: {payload.title}")
```

**Features:**
- **Type Safety:** Automatic Pydantic validation of payloads
- **AsyncAPI:** Auto-generated documentation for consumers
- **Dependency Injection:** Support for testable dependencies
- **Resilience:** Built-in connection management and retries

---

## 3. Event Domains

### 3.1 Fireflies (Meeting Transcription)

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/events/domains/fireflies.py`

**Event Flow:**
```
fireflies.transcript.upload     (Request transcription)
         ↓
fireflies.transcript.ready      (Fireflies webhook: transcript complete)
         ↓
fireflies.transcript.processed  (RAG ingestion complete)
         ↓
fireflies.transcript.failed     (Error at any stage)
```

**Key Payloads:**

1. **FirefliesTranscriptUploadPayload** - Request transcription
   ```python
   {
     "media_file": "s3://bucket/recording.mp3",
     "media_duration_seconds": 3600,
     "media_type": "audio/mpeg",
     "title": "Team Standup",
     "user_id": "user_123"
   }
   ```

2. **FirefliesTranscriptReadyPayload** - Webhook from Fireflies (includes FULL transcript)
   ```python
   {
     "id": "ff_meeting_456",
     "title": "Team Standup",
     "duration": 60.5,
     "sentences": [ /* TranscriptSentence objects */ ],
     "summary": "Key discussion points...",
     "participants": [ /* MeetingParticipant objects */ ],
     "user": { /* FirefliesUser */ }
   }
   ```

3. **FirefliesTranscriptProcessedPayload** - RAG ingestion complete
   ```python
   {
     "transcript_id": "ff_meeting_456",
     "rag_document_id": "doc_789",
     "sentence_count": 234,
     "chunk_count": 47,
     "embedding_model": "text-embedding-ada-002",
     "vector_store": "chroma"
   }
   ```

### 3.2 Agent Thread (LLM Interactions)

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/events/domains/agent/thread.py`

**Event Flow:**
```
agent.thread.prompt (Command)    → Execute prompt
         ↓
agent.thread.response (Fact)     → LLM responded
         ↓
agent.thread.error (Fact)        → Call failed
```

**Key Payloads:**

1. **AgentThreadPrompt** (Command - Invokable)
   ```python
   {
     "provider": "anthropic",
     "model": "claude-sonnet-4",
     "prompt": "Explain event sourcing",
     "project": "bloodbank",
     "working_dir": "/home/user/code/bloodbank"
   }
   ```

2. **AgentThreadResponse** (Fact/Side Effect)
   ```python
   {
     "provider": "anthropic",
     "response": "Event sourcing is...",
     "model": "claude-sonnet-4",
     "tokens_used": 1500,
     "duration_ms": 2340
   }
   ```

### 3.3 Artifact (File Lifecycle)

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/events/domains/artifact.py`

**Events:**
```
artifact.created    - New file/document created
artifact.updated    - File modified
artifact.deleted    - File removed
artifact.ingestion.failed - RAG ingestion failed
```

**Key Payload:**
```python
{
  "action": "created",
  "kind": "transcript",
  "uri": "file:///home/user/transcripts/meeting_123.txt",
  "title": "Team Standup Transcript",
  "content": "...",  # Optional full content
  "metadata": {
    "size_bytes": 45678,
    "mime_type": "text/plain"
  }
}
```

### 3.4 GitHub Integration

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/events/domains/github.py`

**Events:**
```
github.pr.created   - Pull request opened
```

**Key Payload (Cache-based pattern):**
```python
{
  "cache_key": "trinote|423",
  "cache_type": "redis"
}
```

The actual PR data is stored in Redis cache to avoid large message sizes. Consumers fetch from cache using the key.

---

## 4. Deployment Architecture

### 4.1 Service Topology

```
┌─────────────────────────────────────────────────────────────┐
│                       Docker Compose Stack                   │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────┐    │
│  │  Bloodbank  │───▶│  RabbitMQ    │◀───│ Command    │    │
│  │  HTTP API   │    │  (3.x)       │    │ Processor  │    │
│  │  :8682      │    │  :5672       │    │            │    │
│  └─────┬───────┘    │  :15672 (UI) │    └────────────┘    │
│        │            └──────────────┘                        │
│        │                                                     │
│        │            ┌──────────────┐                        │
│        └───────────▶│  Redis       │                        │
│                     │  (7.x)       │                        │
│                     │  :6379       │                        │
│                     └──────────────┘                        │
│                                                               │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────┐    │
│  │  Consumer   │───▶│  RabbitMQ    │◀───│ n8n        │    │
│  │  Services   │    │  (subscribe) │    │ Workflows  │    │
│  └─────────────┘    └──────────────┘    └────────────┘    │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 Configuration

**Location:** `/home/delorenj/code/bloodbank/trunk-main/config.py`

```python
class Settings(BaseSettings):
    # Service info
    service_name: str = "bloodbank"
    environment: str = "dev"

    # RabbitMQ settings
    rabbit_url: str = "amqp://guest:guest@rabbitmq:5672/"
    exchange_name: str = "bloodbank.events.v1"

    # Redis settings (for correlation tracking)
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None
    correlation_ttl_days: int = 30

    # HTTP server settings
    http_host: str = "0.0.0.0"
    http_port: int = 8682
```

### 4.3 Deployment Modes

#### Development (Local)

```bash
# Start dependencies
docker-compose up -d rabbitmq redis

# Run HTTP API
uvicorn event_producers.http:app --reload --port 8682

# Run command processor
python event_producers/command_processor.py
```

#### Production (Docker)

```yaml
# docker-compose.yml
services:
  bloodbank:
    build: .
    ports:
      - "8682:8682"
    environment:
      - RABBIT_URL=amqp://guest:guest@rabbitmq:5672/
      - REDIS_HOST=redis
      - REDIS_PORT=6379
    depends_on:
      - redis
      - rabbitmq

  command_processor:
    build: .
    command: python event_producers/command_processor.py
    depends_on:
      - rabbitmq
      - redis

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data

  rabbitmq:
    image: rabbitmq:3-management-alpine
    ports:
      - "5672:5672"
      - "15672:15672"
    volumes:
      - rabbitmq_data:/var/lib/rabbitmq
```

---

## 5. Advanced Features

### 5.1 MCP Server Integration

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/mcp_server.py`

FastMCP server for AI agents (Claude, GPT, etc.) to publish events:

```python
mcp = FastMCP("bloodbank")
publisher = Publisher(enable_correlation_tracking=True)

@mcp.tool()
async def publish_llm_prompt(
    provider: str,
    prompt: str,
    model: Optional[str] = None,
    ...
) -> Dict[str, Any]:
    """Publish an llm.prompt event to the bus."""
    ev = LLMPrompt(provider=provider, prompt=prompt, model=model)
    source = create_source(host="bloodbank", trigger_type="manual", app="mcp")
    env = create_envelope("llm.prompt", ev, source=source)
    await publisher.publish("llm.prompt", env.model_dump())
    return {"event_id": env.event_id}
```

AI agents can connect to this MCP server to:
- Publish prompt events
- Publish response events with correlation
- Publish artifact events (created/updated files)

### 5.2 TUI (Terminal User Interface)

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/tui/app.py`

Textual-based TUI for managing events:

```
┌─────────────────────────────────────────────────────────────┐
│                    Bloodbank Event Bus TUI                   │
├───────────────┬─────────────────────────────┬───────────────┤
│ Event Browser │      Schema Viewer          │ Payload Editor│
│               │                             │               │
│ ▸ fireflies   │ Event: fireflies.transcript │ {             │
│   ▸ transcript│        .ready               │   "id": "...", │
│     • ready   │                             │   "title": "..."│
│     • upload  │ Properties:                 │ }             │
│     • failed  │   id: string (required)     │               │
│               │   title: string (required)  │               │
│ ▸ agent       │   duration: number          │               │
│   ▸ thread    │   ...                       │               │
│     • prompt  │                             │               │
│     • response│                             │               │
├───────────────┴─────────────────────────────┴───────────────┤
│ [Validate] [Publish] [Load Mock] [Save Mock]                │
├───────────────────────────────────────────────────────────────┤
│ Status: Event published successfully                          │
└───────────────────────────────────────────────────────────────┘
```

**Features:**
- Tree navigation of all event domains
- JSON schema viewer for each event type
- Payload editor with validation
- Mock data loading/saving
- Event publishing
- Real-time status updates

**Run:**
```bash
python -m event_producers.tui.app
```

### 5.3 CLI Tool

**Location:** `/home/delorenj/code/bloodbank/trunk-main/event_producers/cli.py`

Command-line interface for event management:

```bash
# List all events
bb list-events

# List events by domain
bb list-events --domain fireflies

# List only command events
bb list-commands

# Show event schema
bb show fireflies.transcript.ready

# Publish event with mock data
bb publish fireflies.transcript.ready --mock

# Publish agent prompt
bb publish_prompt anthropic claude-sonnet-4 "Explain event sourcing"

# Wrap LLM CLI and capture stdin/stdout
bb wrap claude -- chat --model sonnet-4
```

---

## 6. Patterns and Best Practices

### 6.1 Event Naming Convention

**Format:** `domain.entity.action`

**Rules:**
- **Facts (Events):** Use past tense - `fireflies.transcript.ready`
- **Commands:** Use imperative - `agent.thread.prompt`
- **Nested domains:** Use `/` in code, `.` in routing key - `agent/thread` → `agent.thread.prompt`

### 6.2 Event Versioning

**Strategy:** Non-breaking evolution

```python
# V1 (current)
class FirefliesTranscriptReadyPayload(BaseModel):
    id: str
    title: str

# V2 (add optional field - non-breaking)
class FirefliesTranscriptReadyPayload(BaseModel):
    id: str
    title: str
    speaker_count: Optional[int] = None  # New field

# V3 (breaking change - new event type)
# Create: FirefliesTranscriptReadyPayloadV2
# Routing key: fireflies.transcript.ready.v2
```

### 6.3 Correlation Tracking Patterns

**Pattern 1: Linear Chain**
```
Upload Event (A)
    ↓ correlation_ids=[A]
Ready Event (B)
    ↓ correlation_ids=[B]
Processed Event (C)
```

**Pattern 2: Fan-out**
```
Transcript Ready (A)
    ↓ correlation_ids=[A]
    ├─→ RAG Ingestion (B)
    ├─→ Notification (C)
    └─→ Analytics (D)
```

**Pattern 3: Aggregation**
```
Event A ─┐
Event B ─┼─→ Aggregated Event (correlation_ids=[A,B,C])
Event C ─┘
```

### 6.4 Idempotency Pattern

```python
# Generate deterministic event ID
event_id = publisher.generate_event_id(
    "fireflies.transcript.upload",
    meeting_id="abc123",
    user_id="user_456"
)

# Subsequent publishes with same inputs produce same event_id
# Consumers can dedupe based on event_id
```

### 6.5 Cache-based Payload Pattern

For large payloads (e.g., GitHub PR data):

```python
# 1. Store payload in cache
await redis.setex(f"github:pr:{pr_number}", 3600, orjson.dumps(pr_data))

# 2. Publish event with cache reference
payload = GitHubPRCreatedPayload(
    cache_key=f"trinote|{pr_number}",
    cache_type="redis"
)

# 3. Consumer fetches from cache
pr_data = await redis.get(payload.cache_key)
```

### 6.6 Error Handling Pattern

**Transient Failures:**
```python
try:
    await publisher.publish(...)
except (ConnectionError, TimeoutError) as e:
    # Retry with exponential backoff
    await retry_with_backoff(publish_fn, max_retries=3)
```

**Business Logic Failures:**
```python
# Publish failure event
failure_event = FirefliesTranscriptFailedPayload(
    failed_stage="processing",
    error_message=str(e),
    transcript_id=transcript_id,
    retry_count=attempt,
    is_retryable=True
)
await publish_event(failure_event)
```

---

## 7. Testing

### 7.1 Test Structure

**Location:** `/home/delorenj/code/bloodbank/trunk-main/tests/`

```
tests/
├── __init__.py
├── conftest.py                    # Pytest fixtures
├── test_correlation_tracking.py   # Correlation tracker tests
├── test_command_flow.py           # Command execution tests
└── test_import_compatibility.py   # Backward compatibility tests
```

### 7.2 Testing Patterns

**Unit Test Example:**
```python
# tests/test_correlation_tracking.py
@pytest.mark.asyncio
async def test_add_correlation(redis_tracker):
    parent_id = uuid4()
    child_id = uuid4()

    await redis_tracker.add_correlation(
        child_event_id=child_id,
        parent_event_ids=[parent_id]
    )

    parents = await redis_tracker.get_parents(child_id)
    assert parent_id in parents
```

**Integration Test Example:**
```python
# tests/test_command_flow.py
@pytest.mark.asyncio
async def test_command_execution():
    # Publish command event
    prompt = AgentThreadPrompt(provider="test", prompt="hello")
    envelope = create_envelope("agent.thread.prompt", prompt, source)
    await publisher.publish("agent.thread.prompt", envelope.model_dump())

    # Wait for side effect
    await asyncio.sleep(1)

    # Verify response event was published
    response_messages = await consume_messages("agent.thread.response")
    assert len(response_messages) == 1
```

### 7.3 Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=event_producers --cov-report=html

# Run specific test file
pytest tests/test_correlation_tracking.py

# Run with verbose output
pytest -v
```

---

## 8. Monitoring and Observability

### 8.1 Key Metrics to Monitor

**Publisher Metrics:**
- Events published per second (by event type)
- Publishing latency (p50, p95, p99)
- Correlation tracking success rate
- Redis connection pool utilization

**Consumer Metrics:**
- Messages consumed per second (by service)
- Processing latency (per event type)
- Error rate (by error type)
- Queue depth (per consumer)

**Infrastructure Metrics:**
- RabbitMQ connection count
- RabbitMQ message throughput
- Redis memory usage
- Redis operation latency

### 8.2 Logging Strategy

```python
# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger.info(
    "Event published",
    extra={
        "event_type": envelope.event_type,
        "event_id": str(envelope.event_id),
        "correlation_ids": [str(c) for c in envelope.correlation_ids],
        "source": envelope.source.app
    }
)
```

### 8.3 Debug Endpoints

```python
# Get correlation chain
GET /debug/correlation/{event_id}/chain?direction=ancestors

# Get correlation metadata
GET /debug/correlation/{event_id}

# Dump full correlation data
GET /debug/correlation/{event_id}/dump
```

---

## 9. Security Considerations

### 9.1 Authentication

**Current State:** No authentication (internal network only)

**Production Recommendations:**
- API Gateway with OAuth2/JWT
- RabbitMQ user/password authentication
- Redis password protection
- TLS/SSL for all connections

### 9.2 Authorization

**Event-level Permissions:**
```python
# Future: Role-based event publishing
@app.post("/events/{event_type}")
async def publish_event(
    event_type: str,
    payload: dict,
    current_user: User = Depends(get_current_user)
):
    # Check user can publish this event type
    if not current_user.can_publish(event_type):
        raise HTTPException(403, "Forbidden")

    await publisher.publish(event_type, payload)
```

### 9.3 Input Validation

**Pydantic Validation:**
```python
# Automatic validation via Pydantic
class FirefliesTranscriptReadyPayload(BaseModel):
    id: str = Field(..., min_length=1, max_length=255)
    duration: float = Field(..., ge=0, le=86400)  # Max 24 hours
    sentences: List[TranscriptSentence] = Field(..., max_items=10000)
```

**Schema Validation:**
```python
# Validate against JSON schema
schema = registry.get_schema("fireflies.transcript.ready")
validate(instance=payload, schema=schema)
```

---

## 10. Performance Characteristics

### 10.1 Latency Profile

**Publisher (with correlation tracking):**
- Message serialization: ~0.5ms
- Redis correlation write: ~1-2ms
- RabbitMQ publish: ~2-5ms
- **Total: ~5-10ms** (p95)

**Publisher (without correlation tracking):**
- **Total: ~3-5ms** (p95)

**Consumer:**
- Message deserialization: ~0.5ms
- Envelope parsing: ~0.5ms
- Business logic: Variable
- **Overhead: ~1ms** (p95)

### 10.2 Throughput

**Single Publisher:**
- ~200 events/sec (with correlation tracking)
- ~500 events/sec (without correlation tracking)

**Horizontal Scaling:**
- Add more publisher instances for higher throughput
- RabbitMQ handles 10K+ messages/sec easily
- Redis handles 100K+ ops/sec

### 10.3 Memory Usage

**Publisher:**
- Base: ~50MB
- Per connection: ~5MB
- Correlation tracking: Negligible (Redis-backed)

**Redis (Correlation Tracking):**
- Per event: ~1KB
- 100K events/day × 30 days: ~70MB
- 1M events/day × 30 days: ~700MB

---

## 11. Troubleshooting Guide

### 11.1 Common Issues

**Issue: Events not being consumed**

```bash
# Check RabbitMQ queues
curl -u guest:guest http://localhost:15672/api/queues

# Check consumer logs
docker logs bloodbank_consumer

# Check queue bindings
rabbitmqctl list_bindings
```

**Issue: Correlation tracking not working**

```bash
# Verify Redis connection
redis-cli ping

# Check Redis keys
redis-cli keys "bloodbank:correlation:*"

# Check publisher configuration
grep "enable_correlation_tracking" event_producers/http.py

# Check logs for warnings
docker logs bloodbank_api | grep "Correlation tracking"
```

**Issue: High latency**

```bash
# Check Redis latency
redis-cli --latency

# Check RabbitMQ performance
rabbitmq-diagnostics check_performance

# Check publisher metrics
curl http://localhost:8682/metrics
```

### 11.2 Debug Commands

```bash
# Inspect RabbitMQ exchange
rabbitmqctl list_exchanges

# Inspect queue messages
rabbitmqctl list_queues name messages_ready messages_unacknowledged

# Check correlation data
redis-cli HGETALL bloodbank:correlation:forward:<event-id>

# Tail logs
docker logs -f bloodbank_api
docker logs -f bloodbank_consumer
```

---

## 12. Migration and Upgrade Strategy

### 12.1 From Legacy (Pre-registry) to v2.0

**Location:** `/home/delorenj/code/bloodbank/trunk-main/docs/MIGRATION_v1_to_v2.md`

**Steps:**
1. Enable registry auto-discovery
2. Update imports from old `events.py` to new domain modules
3. Enable correlation tracking (optional)
4. Update envelope creation calls
5. Test backward compatibility

### 12.2 Zero-Downtime Deployment

**Blue-Green Strategy:**
1. Deploy new version alongside old (different exchange)
2. Configure consumers to listen to both exchanges
3. Switch publishers to new exchange
4. Drain old exchange
5. Remove old version

### 12.3 Schema Evolution

**Non-breaking Changes (Safe):**
- Add optional fields
- Add new event types
- Increase field max lengths

**Breaking Changes (Require Migration):**
- Remove fields
- Change field types
- Rename fields
- Add required fields

**Strategy for Breaking Changes:**
1. Create new event type with version suffix (`.v2`)
2. Publish to both old and new event types during transition
3. Update consumers to handle new type
4. Deprecate old type after transition period

---

## 13. Future Enhancements

### 13.1 Planned Features

**Short-term:**
- [ ] Dead-letter queue handling
- [ ] Event replay mechanism
- [ ] Enhanced TUI with real-time event streaming
- [ ] Prometheus metrics exporter
- [ ] OpenTelemetry tracing integration

**Medium-term:**
- [ ] Event schema versioning system
- [ ] GraphQL query interface for event history
- [ ] Automated event documentation generation
- [ ] Integration with popular observability platforms
- [ ] Event archival to S3/object storage

**Long-term:**
- [ ] Event-sourced state reconstruction
- [ ] CQRS read model projections
- [ ] Multi-region replication
- [ ] Advanced stream processing (windowing, aggregation)
- [ ] ML-based anomaly detection on event streams

### 13.2 Known Limitations

1. **No transactional outbox** - Side effects may be lost if system crashes after DB commit but before event publish
2. **No event replay** - Once consumed and ack'd, events are gone (unless dead-lettered)
3. **Limited dead-letter handling** - Currently logs errors but doesn't route to DLQ
4. **No schema registry** - Schema validation happens at runtime, not publish-time
5. **No event retention** - RabbitMQ doesn't persist consumed messages

---

## 14. References

### 14.1 Key Documentation Files

- `/home/delorenj/code/bloodbank/trunk-main/README.md` - Getting started guide
- `/home/delorenj/code/bloodbank/trunk-main/docs/GREENFIELD_DEPLOYMENT.md` - New deployment guide
- `/home/delorenj/code/bloodbank/trunk-main/docs/MIGRATION_v1_to_v2.md` - Migration guide
- `/home/delorenj/code/bloodbank/trunk-main/docs/EVENT_TYPES.md` - Event schema reference
- `/home/delorenj/code/bloodbank/trunk-main/EventDrivenArchitecture.md` - Architecture patterns
- `/home/delorenj/code/bloodbank/trunk-main/docs/Bloodbank_Event_Schemas.md` - Schema documentation

### 14.2 External Resources

**Technologies:**
- [RabbitMQ Documentation](https://www.rabbitmq.com/documentation.html)
- [Pydantic Documentation](https://docs.pydantic.dev/)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [aio-pika Documentation](https://aio-pika.readthedocs.io/)

**Patterns:**
- [Event Sourcing](https://martinfowler.com/eaaDev/EventSourcing.html)
- [CQRS](https://martinfowler.com/bliki/CQRS.html)
- [Transactional Outbox](https://microservices.io/patterns/data/transactional-outbox.html)
- [Command Query Separation](https://martinfowler.com/bliki/CommandQuerySeparation.html)

### 14.3 Related Projects in 33GOD Ecosystem

- **iMi** - Git worktree management (event publisher)
- **Yi** - Orchestration layer (event publisher/consumer)
- **Jelmore** - PostgreSQL wrapper (event publisher)
- **AgentForge** - Agent template management (event publisher)
- **REPL** - Interactive LLM sessions (event publisher)

---

## Appendix A: Quick Reference

### Event Lifecycle Cheat Sheet

```
┌─────────────────────────────────────────────────────────┐
│ 1. External Trigger (HTTP, CLI, Webhook, File Watch)   │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│ 2. Create Envelope (event_type, payload, source)       │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│ 3. Publish to RabbitMQ (routing_key, body)             │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ├─→ Redis (Correlation Tracking)
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│ 4. RabbitMQ Routes to Queues (topic matching)          │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ├─→ Command Processor (Invokable)
                      ├─→ Event Consumers (Facts)
                      └─→ Integration Services (n8n, etc.)

                      ▼
┌─────────────────────────────────────────────────────────┐
│ 5. Side Effects Published (commands → new events)      │
└─────────────────────────────────────────────────────────┘
```

### Common Code Patterns

**Publish Event:**
```python
from event_producers.events.base import create_envelope, Source, TriggerType
from event_producers.events.domains.fireflies import FirefliesTranscriptReadyPayload
from rabbit import Publisher

publisher = Publisher(enable_correlation_tracking=True)
await publisher.start()

payload = FirefliesTranscriptReadyPayload(id="123", title="Meeting", ...)
source = Source(host="localhost", type=TriggerType.MANUAL, app="my-app")
envelope = create_envelope("fireflies.transcript.ready", payload, source)

await publisher.publish(
    routing_key="fireflies.transcript.ready",
    body=envelope.model_dump()
)
```

**Consume Events (FastStream):**
```python
from event_producers.consumer import broker
from event_producers.events.domains.fireflies import FirefliesTranscriptReadyPayload

# Define a subscriber
@broker.subscriber("my_queue", exchange="bloodbank.events.v1", routing_key="fireflies.#")
async def process(payload: FirefliesTranscriptReadyPayload):
    print(f"Received: {payload.id}")

# Run via CLI: faststream run my_service:app
```

**Create Command:**
```python
from event_producers.events.core.abstraction import BaseCommand, CommandContext, EventCollector

class MyCommand(BaseCommand[MyResult]):
    field1: str
    field2: int

    async def execute(self, context: CommandContext, collector: EventCollector) -> MyResult:
        # Business logic
        result = do_something(self.field1)

        # Create side effect
        side_effect = create_envelope("my.event.happened", payload, source,
                                      correlation_ids=[context.correlation_id])
        collector.add(side_effect)

        return result

ROUTING_KEYS = {
    "MyCommand": "my.command.execute"
}
```

---

## Conclusion

Bloodbank provides a robust, type-safe, and observable event bus for the 33GOD ecosystem. Its registry-based discovery, correlation tracking, and command/event separation make it an excellent foundation for building distributed, event-driven systems.

**Key Strengths:**
- Type-safe event payloads with Pydantic
- Auto-discovery of events via registry system
- Built-in correlation tracking for debugging
- Command/Event separation for clear intent
- Rich tooling (TUI, CLI, MCP server)
- Graceful degradation and error handling

**Next Steps:**
1. Review event schemas for your domain
2. Deploy with greenfield configuration
3. Start publishing events from your services
4. Create consumers for your business logic
5. Monitor correlation chains and debug issues

For questions or contributions, refer to the project repository and documentation links above.
