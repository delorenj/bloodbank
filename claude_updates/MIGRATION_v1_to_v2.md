# Bloodbank v1.0 → v2.0 Migration Guide

This guide helps you migrate from Bloodbank v1.0 to v2.0, which introduces Redis-backed correlation tracking, deterministic event IDs, multiple correlation IDs, and standardized error events.

## What's New in v2.0

### 🆕 Major Features

1. **Redis-backed Correlation Tracking** - Automatic tracking of event causation chains
2. **Deterministic Event IDs** - Generate same UUID for identical events (idempotency)
3. **Multiple Correlation IDs** - Events can have multiple parent events (list instead of single)
4. **Standardized Error Events** - Consistent `.failed` and `.error` event patterns
5. **Debug Endpoints** - HTTP API for querying correlation chains
6. **Improved Fireflies Schemas** - Full webhook payload captured

### ⚠️ Breaking Changes

1. **`correlation_id` → `correlation_ids`** (singular to plural)
2. **`EventEnvelope` now requires `correlation_ids: List[UUID]`** (was Optional[UUID])
3. **`Publisher` requires Redis** (can disable with `enable_correlation_tracking=False`)
4. **New dependency: `redis>=5.0.0`** (must install)

## Migration Steps

### Step 1: Install New Dependencies

```bash
# Update pyproject.toml or requirements.txt
pip install redis>=5.0.0

# Or if using the project:
cd /Users/delorenj/code/projects/33GOD/bloodbank
pip install -e .
```

### Step 2: Setup Redis (if not already running)

```bash
# Check if Redis is running
redis-cli ping
# Should respond: PONG

# If not running, start it:
# macOS (native)
brew services start redis

# macOS (Docker)
docker run -d -p 6379:6379 redis:7-alpine

# Verify
redis-cli ping
```

### Step 3: Update Configuration

Add Redis settings to your `.env` file or environment:

```bash
# .env
RABBIT_URL=amqp://user:pass@localhost:5672/
EXCHANGE_NAME=amq.topic

# NEW: Redis settings
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=  # Leave empty if no password
CORRELATION_TTL_DAYS=30
```

### Step 4: Update Event Payloads

#### Before (v1.0):
```python
from events import EventEnvelope, envelope_for

# Old way
envelope = envelope_for(
    event_type="llm.prompt",
    source="http/192.168.1.1",
    data=llm_prompt_payload,
    correlation_id=parent_event_id  # ← Single UUID
)
```

#### After (v2.0):
```python
from events import EventEnvelope, create_envelope, Source, TriggerType

# New way
envelope = create_envelope(
    event_type="llm.prompt",
    payload=llm_prompt_payload,
    source=Source(
        host="192.168.1.1",
        type=TriggerType.HOOK,
        app="http-api"
    ),
    correlation_ids=[parent_event_id]  # ← List of UUIDs
)
```

**Find and replace pattern:**

```bash
# Find all uses of envelope_for
grep -r "envelope_for" --include="*.py" .

# Update each instance:
# correlation_id=x → correlation_ids=[x]
# source="string" → source=Source(host="...", type=TriggerType.X, app="...")
```

### Step 5: Update Publisher Usage

#### Before (v1.0):
```python
publisher = Publisher()
await publisher.start()

await publisher.publish(
    routing_key="llm.prompt",
    body=envelope.model_dump(),
    message_id=envelope.id
)
```

#### After (v2.0):
```python
# Enable correlation tracking (recommended)
publisher = Publisher(enable_correlation_tracking=True)
await publisher.start()

# Option A: With correlation tracking
await publisher.publish(
    routing_key="llm.prompt",
    body=envelope.model_dump(mode="json"),
    event_id=envelope.event_id,
    parent_event_ids=[previous_event_id]  # Automatic Redis tracking!
)

# Option B: Disable tracking (if you don't want Redis)
publisher = Publisher(enable_correlation_tracking=False)
await publisher.publish(
    routing_key="llm.prompt",
    body=envelope.model_dump(mode="json"),
    event_id=envelope.event_id
)
```

### Step 6: Update Consumers

Consumers need to handle the new `correlation_ids` list:

#### Before (v1.0):
```python
envelope = EventEnvelope[LLMPrompt](**data)
if envelope.correlation_id:
    parent_id = envelope.correlation_id  # Single UUID
```

#### After (v2.0):
```python
envelope = EventEnvelope[LLMPrompt](**data)
if envelope.correlation_ids:
    parent_ids = envelope.correlation_ids  # List of UUIDs
    # Or get first parent:
    first_parent = envelope.correlation_ids[0] if envelope.correlation_ids else None
```

### Step 7: Add Error Event Handling

Add error event publishing to your error handlers:

```python
# Before: Just logged errors
try:
    await process_event(envelope)
except Exception as e:
    logger.error(f"Failed to process: {e}")
    raise

# After: Publish error event
try:
    await process_event(envelope)
except Exception as e:
    logger.error(f"Failed to process: {e}")
    
    # Publish error event
    error_payload = YourErrorPayload(
        failed_stage="processing",
        error_message=str(e),
        is_retryable=isinstance(e, RetryableError),
        retry_count=0
    )
    
    error_envelope = create_envelope(
        event_type="your.event.failed",
        payload=error_payload,
        source=Source(host=socket.gethostname(), type=TriggerType.AGENT, app="consumer"),
        correlation_ids=[envelope.event_id]  # Link to failed event!
    )
    
    await publisher.publish(
        routing_key="your.event.failed",
        body=error_envelope.model_dump(mode="json"),
        event_id=error_envelope.event_id,
        parent_event_ids=[envelope.event_id]
    )
    
    raise
```

### Step 8: Update Fireflies Webhooks (if applicable)

The `FirefliesTranscriptReadyPayload` now captures the full webhook payload:

#### Before (v1.0):
```python
class FirefliesTranscriptReadyPayload(BaseModel):
    meeting_id: str
```

#### After (v2.0):
```python
class FirefliesTranscriptReadyPayload(BaseModel):
    id: str  # meeting_id
    title: str
    sentences: List[TranscriptSentence]  # Full transcript!
    summary: Optional[str]
    # ... many more fields (see events.py)
```

Update your webhook handler to use the new rich payload.

### Step 9: Test Correlation Tracking

Verify correlation tracking works:

```python
from rabbit import Publisher
from uuid import uuid4

# Create publisher
publisher = Publisher(enable_correlation_tracking=True)
await publisher.start()

# Generate deterministic event ID
event_id = publisher.generate_event_id(
    "test.event",
    unique_key="test123"
)
print(f"Event ID: {event_id}")

# Publish parent event
await publisher.publish(
    routing_key="test.parent",
    body={"data": "parent"},
    event_id=event_id
)

# Publish child event
child_id = uuid4()
await publisher.publish(
    routing_key="test.child",
    body={"data": "child"},
    event_id=child_id,
    parent_event_ids=[event_id]
)

# Query correlation
chain = publisher.get_correlation_chain(child_id, "ancestors")
print(f"Correlation chain: {chain}")
# Should print: [event_id, child_id]

# Or use HTTP API
# curl http://localhost:8682/debug/correlation/{child_id}
```

### Step 10: Update Tests

Update your test fixtures:

```python
# Before
def create_test_envelope():
    return EventEnvelope(
        event_type="test.event",
        correlation_id=None,  # ← Old
        payload=TestPayload()
    )

# After
def create_test_envelope():
    return EventEnvelope(
        event_type="test.event",
        correlation_ids=[],  # ← New (empty list)
        source=Source(host="test", type=TriggerType.MANUAL, app="test"),
        payload=TestPayload()
    )
```

## Backwards Compatibility

### Using v2.0 Without Redis

If you don't want to use Redis, you can disable correlation tracking:

```python
publisher = Publisher(enable_correlation_tracking=False)
```

**Note:** This disables:
- Automatic correlation tracking
- Deterministic event ID generation via `generate_event_id()`
- Correlation chain queries via `get_correlation_chain()`

You can still manually set `correlation_ids` in envelopes - they just won't be tracked in Redis.

### Using Old `envelope_for` Function

The old `envelope_for()` function still exists for backwards compatibility:

```python
# Still works, but deprecated
envelope = envelope_for(
    event_type="llm.prompt",
    source="http/192.168.1.1",
    data=llm_prompt_payload,
    correlation_id=parent_id
)
# Internally converts correlation_id to correlation_ids=[correlation_id]
```

**Recommendation:** Migrate to `create_envelope()` for full v2.0 features.

## Rollback Plan

If you need to rollback:

1. **Don't update event_producers/events.py** - keep v1.0 schemas
2. **Don't update rabbit.py** - keep v1.0 Publisher
3. **Keep using v1.0 patterns** - `correlation_id`, `envelope_for()`, etc.

Or:

```bash
# Checkout v1.0 files
git checkout v1.0 event_producers/events.py event_producers/rabbit.py
pip uninstall redis
```

## Common Issues

### Issue: `ModuleNotFoundError: No module named 'redis'`

**Solution:**
```bash
pip install redis>=5.0.0
```

### Issue: `ConnectionError: Failed to connect to Redis`

**Solution:**
```bash
# Check Redis is running
redis-cli ping

# Start Redis
brew services start redis  # macOS
sudo systemctl start redis  # Linux
```

### Issue: `AttributeError: 'EventEnvelope' object has no attribute 'correlation_id'`

**Solution:** Update code to use `correlation_ids` (plural):
```python
# Before
parent = envelope.correlation_id

# After
parents = envelope.correlation_ids
parent = parents[0] if parents else None
```

### Issue: `TypeError: create_envelope() missing required argument 'source'`

**Solution:** Update from `envelope_for()` to `create_envelope()`:
```python
# Before
envelope = envelope_for(event_type="...", source="string", data=...)

# After
envelope = create_envelope(
    event_type="...",
    payload=...,
    source=Source(host="...", type=TriggerType.X, app="...")
)
```

## Checklist

Use this checklist to track your migration:

- [ ] Installed Redis dependency (`pip install redis>=5.0.0`)
- [ ] Redis server is running (`redis-cli ping` → PONG)
- [ ] Updated `.env` with Redis settings
- [ ] Updated all `envelope_for()` calls to `create_envelope()`
- [ ] Changed `correlation_id` to `correlation_ids` (list)
- [ ] Updated `Publisher` initialization with `enable_correlation_tracking=True`
- [ ] Updated `publish()` calls to use `parent_event_ids` parameter
- [ ] Updated consumers to handle `correlation_ids` as list
- [ ] Added error event publishing to error handlers
- [ ] Updated tests to use new schemas
- [ ] Tested correlation tracking with `get_correlation_chain()`
- [ ] Tested debug endpoints (`/debug/correlation/{event_id}`)

## Questions?

- Check the updated SKILL.md for comprehensive documentation
- Use debug endpoints to inspect correlation data
- Review the example files in `event_producers/scripts/`

## Version Compatibility

| Feature | v1.0 | v2.0 |
|---------|------|------|
| correlation_id (singular) | ✅ | ⚠️ Deprecated (use correlation_ids) |
| correlation_ids (plural) | ❌ | ✅ |
| Redis correlation tracking | ❌ | ✅ |
| Deterministic event IDs | ❌ | ✅ |
| Error event patterns | ❌ | ✅ |
| Debug endpoints | ❌ | ✅ |
| envelope_for() | ✅ | ⚠️ Deprecated (use create_envelope) |
| create_envelope() | ❌ | ✅ |
