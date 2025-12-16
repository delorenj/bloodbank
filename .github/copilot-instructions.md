# Bloodbank Event System - GitHub Copilot Instructions

## Repository Overview

This repository contains the **Bloodbank Event Bus**, a RabbitMQ-based event-driven system for the 33GOD ecosystem. Bloodbank facilitates communication between different systems using an event-driven architecture, enabling loose coupling, scalability, and flexibility.

### Core Components

1. **Bloodbank Event Bus**: Generic, RabbitMQ-based event bus infrastructure
2. **Event Producers**: Collection of tools and services that produce events (FastAPI server, CLI, MCP server, n8n workflows)

## Event-Driven Architecture Principles

### Event Naming Conventions

Events in Bloodbank follow strict naming patterns:

- **Standard Events** (immutable, past-tense): `<domain>.<entity>.<past-tense-action>`
  - Example: `github.pr.created`, `fireflies.transcript.ready`
  - These notify that something has happened
  
- **Command Events** (mutation requests, present-tense): `<domain>.<entity>.<action>`
  - Example: `github.pr.merge`, `agent.task.execute`
  - These have dedicated exchanges and worker queues
  - They request an action to be performed

### Event Structure

All events use the `EventEnvelope[T]` wrapper with:
- `event_id`: UUID (can be deterministic for deduplication)
- `event_type`: Routing key for RabbitMQ
- `timestamp`: ISO 8601 UTC timestamp
- `version`: Envelope schema version (currently "1.0.0")
- `source`: Metadata about who/what triggered the event
  - `host`: Machine name
  - `type`: TriggerType enum (MANUAL, AGENT, SCHEDULED, FILE_WATCH, HOOK)
  - `app`: Optional application name
  - `meta`: Additional context
- `correlation_ids`: List of parent event UUIDs for causation tracking
- `agent_context`: Optional rich metadata when source is an AI agent
- `payload`: Typed event data (your Pydantic model)

### Event Payload Definitions

Event payloads are defined as Pydantic models in `event_producers/events/domains/` organized by domain:

```
event_producers/events/
├── base.py           # EventEnvelope and core types
├── registry.py       # Auto-discovery and type registry
├── utils.py          # Helper functions
└── domains/
    ├── __init__.py
    ├── github.py     # GitHub-related events
    ├── agent_thread.py  # AI agent events
    └── fireflies.py  # Meeting transcript events
```

Each domain module must:
1. Define Pydantic payload models
2. Include a `ROUTING_KEYS` dictionary mapping class names to routing keys
3. Add docstrings explaining when events are published/consumed

Example:
```python
class GitHubPRCreatedPayload(BaseModel):
    """Published when a GitHub PR is created."""
    cache_key: str
    cache_type: Literal["redis", "memory", "file"] = "redis"

ROUTING_KEYS = {
    "GitHubPRCreatedPayload": "github.pr.created",
}
```

## Technology Stack

### Core Infrastructure
- **Python 3.11+**: Primary language
- **RabbitMQ**: Message broker (exchange: `bloodbank.events.v1`)
- **aio-pika**: Asynchronous RabbitMQ client
- **Pydantic 2.x**: Data validation and settings
- **Redis**: Correlation tracking and caching
- **FastAPI**: HTTP endpoints for event publishing
- **Typer**: CLI interface

### Development Tools
- **uv**: Package management (`uv run` for commands)
- **pytest**: Testing framework
- **mise**: Development environment management

## Key Files and Modules

### Core Infrastructure
- `rabbit.py`: `Publisher` class for RabbitMQ connections
- `config.py`: Pydantic settings from environment variables
- `correlation_tracker.py`: Tracks event causation chains

### Event Producers
- `event_producers/http.py`: FastAPI server (port 8682)
- `event_producers/cli.py`: Typer CLI for event publishing
- `event_producers/mcp_server.py`: MCP server providing event publishing tools
- `event_producers/watch.py`: File watcher for artifact events
- `event_producers/events/`: Event payload definitions and registry

### Infrastructure
- `kubernetes/deploy.yaml`: Kubernetes deployment configuration
- `pyproject.toml`: Python dependencies and project metadata
- `.mise.toml`: Development environment configuration

## Development Workflows

### Working with Events

1. **Define New Event Type**:
   - Add payload model to appropriate domain in `event_producers/events/domains/`
   - Update `ROUTING_KEYS` dictionary
   - Include comprehensive docstring
   - Registry auto-discovers on startup

2. **Publish Events**:
   ```python
   from rabbit import Publisher
   from event_producers.events.base import create_envelope, Source, TriggerType
   from event_producers.events.domains.github import GitHubPRCreatedPayload
   
   publisher = Publisher()
   await publisher.start()
   
   payload = GitHubPRCreatedPayload(cache_key="repo|123", cache_type="redis")
   source = Source(host="localhost", type=TriggerType.HOOK, app="github-webhook")
   envelope = create_envelope("github.pr.created", payload, source)
   
   await publisher.publish("github.pr.created", envelope.model_dump_json())
   ```

3. **Discover Available Events**:
   ```python
   # Use the event registry programmatically
   from event_producers.events.registry import get_registry
   
   registry = get_registry()
   domains = registry.list_domains()
   for domain in domains:
       events = registry.list_domain_events(domain)
       print(f"{domain}: {events}")
   
   # Get schema for specific event
   schema = registry.get_schema("github.pr.created")
   ```

### Running Services

```bash
# FastAPI HTTP server (for webhooks)
uvicorn event_producers.http:app --reload --port 8682

# MCP server (for internal tools)
python -m event_producers.mcp_server

# File watcher
python -m event_producers.watch /path/to/watch

# Install dependencies
uv sync
```

### Testing

```bash
# Run tests
pytest

# Run specific test
pytest tests/test_events.py

# Check imports
python test_import_compatibility.py
```

## n8n Workflow Integration

### Shell Context Independence Pattern

**CRITICAL**: n8n Execute Command nodes run in subprocess environments without shell aliases/functions.

**Requirements**:
1. Self-contained scripts in `~/.local/bin/`
2. Explicit PATH exports in script headers
3. No dependencies on `.zshrc`/`.bashrc`
4. Detached execution for long-running operations

### jelmore CLI Integration

**jelmore** is the preferred execution primitive for LLM invocations in n8n workflows.

**Key Features**:
- ✅ Shell-context-free execution
- ✅ Immediate return with session handle (non-blocking)
- ✅ Convention over configuration
- ✅ Built-in iMi worktree integration
- ✅ Native Bloodbank event publishing
- ✅ Detached Zellij sessions for observability

**Execute Command Node Pattern**:
```javascript
{
  "command": "uv run jelmore execute -f /path/to/task.md --worktree pr-{{ $json.pr_number }} --auto --json",
  "timeout": 5000
}
```

**Immediate Response** (non-blocking):
```json
{
  "execution_id": "abc123",
  "session_name": "jelmore-pr-458-20251103-143022",
  "client": "claude-flow",
  "log_path": "/tmp/jelmore-abc123.log",
  "working_directory": "/home/user/code/n8n/pr-458",
  "started_at": "2025-11-03T14:30:22"
}
```

### Event-Driven Coordination

jelmore automatically publishes lifecycle events to Bloodbank:

**Execution Lifecycle Events**:
- `jelmore.execution.started` → Task begins
- `jelmore.execution.progress` → Periodic status updates
- `jelmore.execution.completed` → Task finished successfully
- `jelmore.execution.failed` → Task encountered error

**Multi-Phase Workflow Pattern**:

```
┌─────────────────────────────────────┐
│  n8n Workflow (Execute Command)     │
│  - Triggers jelmore execution       │
│  - Gets immediate response          │
│  - Continues to next node           │
└─────────┬───────────────────────────┘
          │ (jelmore.execution.started)
          ▼
┌─────────────────────────────────────┐
│  Bloodbank Event Bus                │
│  - Routes events to subscribers     │
└─────────┬───────────────────────────┘
          │ (subscribe to events)
          ▼
┌─────────────────────────────────────┐
│  n8n Webhook (Separate Workflow)    │
│  - Listens for completion events    │
│  - Processes results                │
└─────────────────────────────────────┘
```

**Configuration-Based Pattern**:
```bash
uv run jelmore execute --config n8n-pr-review.json \
  --var PR_NUMBER={{ $json.pr_number }} \
  --var WORKFLOW_ID={{ $workflow.id }} \
  --json
```

Store reusable configs in `~/.config/jelmore/profiles/` or `.jelmore/` in project.

## Code Style and Conventions

### Python Code Standards
- Use Python 3.11+ features
- Type hints for all function signatures
- Pydantic models for data validation
- Async/await for I/O operations
- Comprehensive docstrings (Google style)

### Event Design Guidelines
- Events are immutable - never modify published events
- Use deterministic `event_id` for deduplication:
  ```python
  from correlation_tracker import CorrelationTracker
  tracker = CorrelationTracker()
  event_id = tracker.generate_event_id("github.pr.created", unique_key="repo|pr|123")
  ```
- Always include `correlation_ids` to track causation chains
- Cache-based pattern: Store large payloads in Redis, reference via `cache_key`
- Document when events are published and who consumes them

### Adding New Events
1. Choose appropriate domain (or create new one)
2. Define Pydantic payload model with docstring
3. Add to `ROUTING_KEYS` dictionary
4. No need to manually register - auto-discovered on startup
5. Update documentation if creating new domain

### Testing Events
```python
from event_producers.events.registry import get_registry

registry = get_registry()
assert registry.is_valid_event_type("github.pr.created")
payload_type = registry.get_payload_type("github.pr.created")
schema = registry.get_schema("github.pr.created")
```

## Common Patterns

### Publishing via HTTP (FastAPI)
```bash
curl -X POST http://localhost:8682/events/github/pr/created \
  -H "Content-Type: application/json" \
  -d '{"cache_key": "repo|123", "cache_type": "redis"}'
```

### Publishing via MCP Server
MCP tools are available to other services for publishing events programmatically.

### Subscribing to Events
See `subscriber_example.py` for template consumer service.

```python
# Listen to specific events
binding_key = "github.pr.created"

# Listen to all GitHub events
binding_key = "github.*"

# Listen to everything
binding_key = "#"
```

## Important Notes

- **RabbitMQ Exchange**: `bloodbank.events.v1` (topic exchange)
- **HTTP Server Port**: 8682
- **Event Discovery**: Auto-discovery via registry on startup
- **Correlation Tracking**: Redis-based, 30-day TTL by default
- **Environment Config**: Use `.env` file or environment variables (see `config.py`)

## Related Documentation

- `README.md`: User-facing documentation and quick start
- `TASK.md`: Current development tasks
- `GEMINI.md`: Gemini AI assistant context
- `docs/`: Additional documentation
- `event_producers/n8n/README.md`: n8n workflow patterns
- `kubernetes/deploy.yaml`: Deployment configuration

## Working with This Repository

When making changes:
1. Understand event-driven architecture principles
2. Follow naming conventions strictly
3. Use Pydantic models for all payloads
4. Include comprehensive docstrings
5. Test event publishing/subscribing
6. Update `ROUTING_KEYS` for new events
7. Consider correlation tracking for causation chains
8. Document integration points
9. Respect shell-context-free patterns for n8n workflows
10. Use jelmore for AI agent task execution in workflows
