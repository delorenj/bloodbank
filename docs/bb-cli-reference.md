# Bloodbank CLI Reference

**Version**: 0.2.0
**Last Updated**: 2026-01-27

## Overview

The `bb` CLI is the primary interface for publishing and managing events in the Bloodbank event bus system. It provides commands for event discovery, publishing, and schema validation.

## Installation

```bash
# Install via uv (recommended)
cd /path/to/bloodbank
uv sync

# The bb command is available after installation
bb --help
```

## Core Commands

### bb publish

Publish an event to the Bloodbank event bus.

**Synopsis**:
```bash
bb publish <EVENT_NAME> [OPTIONS]
```

**Arguments**:
- `EVENT_NAME` - Event type/routing key (e.g., `transcription.voice.completed`)

**Options**:

| Option | Short | Description |
|--------|-------|-------------|
| `--payload-file PATH` | | Path to JSON payload file |
| `--json TEXT` | `-j` | Inline JSON string, `@file`, or `-` for stdin |
| `--envelope-file PATH` | | Path to full envelope JSON file |
| `--envelope-json TEXT` | | Inline envelope JSON string or `@file` |
| `--event-id TEXT` | | Override event_id (UUID) |
| `--correlation-id TEXT` | `-c` | Parent event UUID(s) for correlation tracking |
| `--source-type TYPE` | | Event source: `manual`, `agent`, `scheduled`, `file_watch`, `hook` |
| `--source-app TEXT` | | Source application name (default: `bloodbank-cli`) |
| `--source-host TEXT` | | Source hostname (default: system hostname) |
| `--skip-validation` | | Skip HolyFields schema validation |
| `--strict-validation` | | Fail if schema not found (default: true) |
| `--permissive-validation` | | Allow missing schemas (default: false) |
| `--dry-run` | | Print payload without publishing |
| `--mock` | `-m` | Use mock data from registry (if available) |

**Examples**:

```bash
# Publish with payload file
bb publish transcription.voice.completed --payload-file event.json

# Publish with inline JSON
bb publish transcription.voice.completed --json '{"text":"Hello","timestamp":"2026-01-27T10:00:00Z"}'

# Publish from stdin
echo '{"text":"Hello"}' | bb publish transcription.voice.completed --json -

# Publish with correlation tracking
bb publish transcription.voice.processed \
  --payload-file processed.json \
  --correlation-id parent-event-uuid

# Dry run (validate without publishing)
bb publish transcription.voice.completed \
  --payload-file event.json \
  --dry-run

# Skip validation (not recommended)
bb publish custom.event \
  --payload-file event.json \
  --skip-validation

# Permissive mode (allow missing schemas)
bb publish new.event.type \
  --payload-file event.json \
  --permissive-validation
```

**Schema Validation**:

By default, `bb publish` validates payloads against HolyFields JSON schemas:

1. **Strict Mode** (default): Fails if schema not found
   ```bash
   bb publish transcription.voice.completed --payload-file event.json
   ```

2. **Permissive Mode**: Allows publishing even if schema missing
   ```bash
   bb publish transcription.voice.completed \
     --payload-file event.json \
     --permissive-validation
   ```

3. **Skip Validation**: Bypass all validation (not recommended)
   ```bash
   bb publish transcription.voice.completed \
     --payload-file event.json \
     --skip-validation
   ```

**Validation Output**:

```
Validating payload against HolyFields schema...
✓ Schema validation passed (https://33god.dev/schemas/whisperlivekit/events/transcription.v1.json)
✓ Published transcription.voice.completed (event_id: 123e4567-e89b-12d3-a456-426614174000)
```

### bb list-events

List all available events registered in the system.

**Synopsis**:
```bash
bb list-events [OPTIONS]
```

**Options**:
- `--domain TEXT` / `-d` - Filter by domain (e.g., `fireflies`, `agent`)
- `--type TEXT` / `-t` - Filter by type: `event` or `command`

**Examples**:

```bash
# List all events
bb list-events

# List events in specific domain
bb list-events --domain fireflies

# List only command events
bb list-events --type command
```

**Output**:

```
Domain: fireflies
  - fireflies.transcript.ready (FirefliesTranscriptReadyEvent)
  - fireflies.transcript.processed (FirefliesTranscriptProcessedEvent)

Domain: agent
  - agent.thread.prompt (AgentThreadPromptEvent)
  - agent.thread.response (AgentThreadResponseEvent)

Total: 15 event(s)
```

### bb list-commands

List all command events (mutable operations).

**Synopsis**:
```bash
bb list-commands [OPTIONS]
```

**Options**:
- `--domain TEXT` / `-d` - Filter by domain

**Examples**:

```bash
# List all commands
bb list-commands

# List commands in specific domain
bb list-commands --domain github
```

### bb show

Show full event definition including schema and mock data.

**Synopsis**:
```bash
bb show <EVENT_NAME>
```

**Arguments**:
- `EVENT_NAME` - Event class name or routing key

**Examples**:

```bash
# Show by class name
bb show FirefliesTranscriptReadyEvent

# Show by routing key
bb show fireflies.transcript.ready
```

**Output**:

```
Event: FirefliesTranscriptReadyEvent
Domain: fireflies
Routing Key: fireflies.transcript.ready
Type: Event

Schema:
{
  "type": "object",
  "properties": {
    "id": {"type": "string", "format": "uuid"},
    "title": {"type": "string"},
    ...
  },
  "required": ["id", "title"]
}

Example (from mock):
{
  "id": "123e4567-e89b-12d3-a456-426614174000",
  "title": "Meeting Transcript",
  ...
}
```

### bb help

Show comprehensive help organized by category.

**Synopsis**:
```bash
bb help
```

**Output**: Displays organized help covering:
- Event vs Command concepts
- Publishing workflow
- Adding new events
- Type system
- Available commands

## Event Structure

### Event Envelope

All events are wrapped in a standardized envelope:

```json
{
  "event_id": "123e4567-e89b-12d3-a456-426614174000",
  "event_type": "transcription.voice.completed",
  "timestamp": "2026-01-27T10:00:00Z",
  "version": "1.0.0",
  "source": {
    "host": "workstation-01",
    "type": "manual",
    "app": "bloodbank-cli"
  },
  "correlation_ids": ["parent-event-uuid"],
  "agent_context": null,
  "payload": {
    "text": "Transcribed content here",
    "timestamp": "2026-01-27T10:00:00Z",
    "session_id": "session-uuid"
  }
}
```

### Payload Structure

The payload structure depends on the event type and is defined in HolyFields schemas:

```json
{
  "text": "Transcribed text",
  "timestamp": "2026-01-27T10:00:00Z",
  "source": "whisperlivekit",
  "target": "tonny",
  "session_id": "session-uuid",
  "audio_metadata": {
    "duration_ms": 5000,
    "sample_rate": 16000
  }
}
```

## Integration Patterns

### Publishing from Python

```python
import asyncio
import json
from pathlib import Path
from event_producers.rabbit import Publisher
from event_producers.events import EventEnvelope, Source, TriggerType
from event_producers.schema_validator import validate_event
from uuid import uuid4
from datetime import datetime, timezone

async def publish_transcription_event(text: str, session_id: str):
    # Create payload
    payload = {
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "whisperlivekit",
        "target": "tonny",
        "session_id": session_id,
        "audio_metadata": {
            "duration_ms": 5000,
            "sample_rate": 16000
        }
    }

    # Validate against schema
    result = validate_event(
        event_type="transcription.voice.completed",
        payload=payload,
        strict=True
    )

    if not result.valid:
        print(f"Validation failed: {result.errors}")
        return

    # Create envelope
    envelope = EventEnvelope(
        event_id=uuid4(),
        event_type="transcription.voice.completed",
        timestamp=datetime.now(timezone.utc),
        version="1.0.0",
        source=Source(
            host="my-service",
            type=TriggerType.AGENT,
            app="my-transcription-service"
        ),
        correlation_ids=[],
        payload=payload
    )

    # Publish
    publisher = Publisher(enable_correlation_tracking=True)
    await publisher.start()

    await publisher.publish(
        routing_key="transcription.voice.completed",
        body=envelope.model_dump(mode="json"),
        event_id=envelope.event_id
    )

    await publisher.close()
    print(f"Published event: {envelope.event_id}")

# Run
asyncio.run(publish_transcription_event("Hello world", "session-123"))
```

### Publishing from Shell Scripts

```bash
#!/bin/bash

# Create payload
cat > /tmp/event.json <<EOF
{
  "text": "Transcribed from shell",
  "timestamp": "$(date -Iseconds)",
  "source": "whisperlivekit",
  "target": "tonny",
  "session_id": "$(uuidgen)"
}
EOF

# Publish event
bb publish transcription.voice.completed \
  --payload-file /tmp/event.json \
  --source-app "my-shell-script" \
  --source-type agent

# Check exit code
if [ $? -eq 0 ]; then
  echo "Event published successfully"
else
  echo "Event publish failed"
  exit 1
fi
```

### Publishing with Correlation Tracking

```bash
# Store parent event ID
PARENT_ID=$(bb publish transcription.voice.started \
  --payload-file started.json \
  --dry-run \
  | grep event_id \
  | cut -d'"' -f4)

# Publish child event with correlation
bb publish transcription.voice.completed \
  --payload-file completed.json \
  --correlation-id "$PARENT_ID"

# Query correlation chain
bb query-correlation "$PARENT_ID"
```

## Error Handling

### Common Errors

**Event not found in registry**:
```
Warning: Event 'custom.event' not found in registry.
Treating as ad-hoc event type (routing key: custom.event)
```

**Solution**: Continue with ad-hoc event (allowed), or register event in `event_producers/events/domains/`.

**Schema validation failed**:
```
Schema validation failed:
  ✗ Missing required field: text
  ✗ Field 'timestamp' has wrong type: expected string, got integer
```

**Solution**: Fix payload to match schema, or use `--permissive-validation` / `--skip-validation`.

**RabbitMQ connection failed**:
```
RuntimeError: Failed to connect to RabbitMQ at 'amqp://192.168.1.12:5672/': [Errno 111] Connection refused
```

**Solution**:
1. Check RabbitMQ is running: `systemctl status rabbitmq-server`
2. Verify connection in `.env` file
3. Check network connectivity: `nc -zv 192.168.1.12 5672`

**Invalid JSON payload**:
```
Error creating event payload: Expecting property name enclosed in double quotes
```

**Solution**: Validate JSON syntax with `jq`:
```bash
cat payload.json | jq .
```

## Best Practices

### 1. Always Use Schema Validation

```bash
# ✓ GOOD - Validates against schema
bb publish transcription.voice.completed --payload-file event.json

# ✗ BAD - Skips validation
bb publish transcription.voice.completed --payload-file event.json --skip-validation
```

### 2. Use Correlation Tracking for Event Chains

```bash
# Link related events together
bb publish transcription.voice.started --payload-file started.json
bb publish transcription.voice.completed \
  --payload-file completed.json \
  --correlation-id <parent-event-id>
```

### 3. Use Dry Run for Testing

```bash
# Test payload before publishing
bb publish transcription.voice.completed \
  --payload-file event.json \
  --dry-run
```

### 4. Specify Source Metadata

```bash
# Provide context about event origin
bb publish transcription.voice.completed \
  --payload-file event.json \
  --source-app "whisperlivekit" \
  --source-type agent \
  --source-host $(hostname)
```

### 5. Store Event IDs for Debugging

```bash
# Capture event ID for later reference
EVENT_ID=$(bb publish transcription.voice.completed \
  --payload-file event.json \
  | grep event_id \
  | cut -d'"' -f4)

echo "Published event: $EVENT_ID"
```

## Configuration

### Environment Variables

Configure Bloodbank via `.env` file or environment variables:

```bash
# RabbitMQ connection
RABBIT_URL=amqp://user:pass@host:5672/

# Redis (for correlation tracking)
REDIS_HOST=localhost
REDIS_PORT=6379

# Exchange name
EXCHANGE_NAME=bloodbank.events.v1
```

### HolyFields Integration

Schema validator auto-discovers HolyFields repository:

1. `../holyfields/trunk-main` (relative to bloodbank)
2. `~/code/33GOD/holyfields/trunk-main`
3. `/home/delorenj/code/33GOD/holyfields/trunk-main`

Or specify explicitly:

```python
from event_producers.schema_validator import SchemaValidator

validator = SchemaValidator(
    holyfields_path="/path/to/holyfields/trunk-main",
    strict=True
)
```

## Troubleshooting

### Check RabbitMQ Connection

```bash
# Test publisher connectivity
python -c "
from event_producers.rabbit import Publisher
import asyncio

async def test():
    pub = Publisher()
    await pub.start()
    print('✓ Connected')
    await pub.close()

asyncio.run(test())
"
```

### Verify Schema Availability

```bash
# Check if HolyFields schemas exist
ls -la ~/code/33GOD/holyfields/trunk-main/whisperlivekit/events/

# Validate schema manually
cat ~/code/33GOD/holyfields/trunk-main/whisperlivekit/events/transcription.v1.schema.json | jq .
```

### Enable Debug Logging

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# Now run bb commands with verbose output
```

## See Also

- [RabbitMQ Infrastructure Documentation](rabbitmq-infrastructure.md)
- [HolyFields Schema Registry](../../holyfields/trunk-main/README.md)
- [Event-Driven Architecture Guide](EventDrivenArchitecture.md)
- [Integration Test Examples](../tests/test_bb_publish_integration.py)

## Acceptance Criteria Status

**STORY-004: Implement bb publish command with schema validation** ✅ COMPLETE

- ✅ CLI command: `bb publish --event-type <type> --payload '{...}'`
- ✅ Payload validated against HolyFields schema before publishing
- ✅ Event includes metadata: timestamp, source, routing key
- ✅ Success/failure feedback to CLI
- ✅ Integration test with HolyFields schema validation
- ✅ Help documentation for command usage (this file)

**Date Completed**: 2026-01-27
**Test Coverage**: 14/14 tests passing
