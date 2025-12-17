# Bloodbank v2.0 - Greenfield Deployment Guide

**For new deployments starting fresh with no legacy systems**

---

## Quick Start

### 1. Prerequisites

Ensure these services are running:
- **Redis 5.0+** - For correlation tracking
- **RabbitMQ** - For message publishing
- **Python 3.11+** - Runtime environment

### 2. Installation

```bash
# Clone the repository
git clone <your-repo-url>
cd bloodbank

# Install dependencies
pip install -r pyproject.toml
# or if using uv:
uv sync
```

### 3. Configuration

Create a `.env` file with your settings:

```bash
# RabbitMQ (Required)
RABBIT_URL=amqp://guest:guest@localhost:5672/
RABBIT_EXCHANGE=events
RABBIT_EXCHANGE_TYPE=topic

# Redis (Required for correlation tracking)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=  # Leave empty if no auth
REDIS_TTL_DAYS=30

# HTTP API
HTTP_HOST=0.0.0.0
HTTP_PORT=8682
```

### 4. Enable Correlation Tracking (RECOMMENDED)

For greenfield deployments, **enable correlation tracking by default**. There's no legacy baggage, so take advantage of the debugging capabilities from day one.

Update `/event_producers/http.py`:

```python
# Change this line:
publisher = Publisher()  # Old default

# To this:
publisher = Publisher(enable_correlation_tracking=True)  # Greenfield default
```

**Why enable by default for greenfield?**
- No backward compatibility concerns
- Immediate debugging capabilities via correlation chains
- Deterministic event IDs for idempotency from the start
- Only adds ~1-2ms latency per publish (negligible)
- Gracefully degrades if Redis is temporarily unavailable

### 5. Start the Services

```bash
# Start the HTTP API
uvicorn event_producers.http:app --host 0.0.0.0 --port 8682

# Or with auto-reload for development
uvicorn event_producers.http:app --reload
```

---

## Verification Checklist

Run through these steps to verify everything works:

### ✅ Service Health

```bash
# 1. Redis is running
redis-cli ping
# Expected: PONG

# 2. RabbitMQ is accessible
curl -u guest:guest http://localhost:15672/api/overview
# Expected: JSON response with RabbitMQ stats

# 3. HTTP API is running
curl http://localhost:8682/healthz
# Expected: {"status": "healthy", "version": "0.2.0"}
```

### ✅ Test Event Publishing

```bash
# Publish a test LLM prompt event
curl -X POST http://localhost:8682/events/llm/prompt \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Test prompt",
    "model": "gpt-4",
    "temperature": 0.7
  }'

# Expected: JSON response with event envelope including event_id
```

### ✅ Verify Correlation Tracking

```bash
# 1. Get the event_id from the previous response
EVENT_ID="<event-id-from-response>"

# 2. Check correlation data was stored
curl http://localhost:8682/debug/correlation/${EVENT_ID}

# Expected: JSON with correlation metadata including timestamp and routing_key
```

### ✅ Test Debug Endpoints

```bash
# Get correlation chain (will be empty for first event)
curl "http://localhost:8682/debug/correlation/${EVENT_ID}/chain?direction=ancestors"

# Expected: {"event_id": "...", "chain": [], "metadata": {...}}
```

---

## Configuration Options

### Core Settings

| Setting | Default | Greenfield Recommendation | Notes |
|---------|---------|---------------------------|-------|
| `enable_correlation_tracking` | `False` | **`True`** | Enable from day 1 |
| `REDIS_TTL_DAYS` | `30` | `30` | Adjust based on volume |
| `REDIS_DB` | `0` | `0` | Use dedicated DB if needed |

### Memory Planning

With correlation tracking enabled:
- **Per event:** ~1KB of Redis memory
- **100K events/day:** ~70MB RAM with 30-day TTL
- **1M events/day:** ~700MB RAM with 30-day TTL

Adjust `REDIS_TTL_DAYS` based on your volume and available memory.

---

## Production Deployment

### Docker Deployment (Recommended)

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml .
RUN pip install -r pyproject.toml

COPY . .

# Enable correlation tracking for greenfield
RUN sed -i 's/Publisher()/Publisher(enable_correlation_tracking=True)/g' event_producers/http.py

CMD ["uvicorn", "event_producers.http:app", "--host", "0.0.0.0", "--port", "8682"]
```

### Docker Compose

```yaml
version: '3.8'

services:
  bloodbank:
    build: .
    ports:
      - "8682:8682"
    environment:
      - RABBIT_URL=amqp://guest:guest@rabbitmq:5672/
      - REDIS_HOST=redis
    depends_on:
      - redis
      - rabbitmq

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data

  rabbitmq:
    image: rabbitmq:3-management-alpine
    ports:
      - "5672:5672"
      - "15672:15672"
    volumes:
      - rabbitmq_data:/var/lib/rabbitmq

volumes:
  redis_data:
  rabbitmq_data:
```

### Kubernetes Deployment

See `k8s/` directory for Helm charts and manifests (if available).

---

## Monitoring

### Essential Metrics

Monitor these from day 1:

1. **Redis Memory Usage**
   ```bash
   redis-cli INFO memory | grep used_memory_human
   ```

2. **Publishing Latency**
   - Track 95th percentile latency on `/events/*` endpoints
   - Alert if >100ms (indicates Redis issues)

3. **Correlation Tracking Success Rate**
   - Monitor warning logs for "Correlation tracking timed out"
   - Alert if >1% timeout rate

4. **Redis Connection Health**
   ```bash
   redis-cli --latency
   ```

---

## Troubleshooting

### Issue: "Correlation tracking not available"

**Solution:** You forgot to enable it. Update `http.py`:
```python
publisher = Publisher(enable_correlation_tracking=True)
```

### Issue: Events publish but no correlation data

**Check:**
1. Redis is running: `redis-cli ping`
2. Publisher has tracking enabled (see above)
3. No timeout warnings in logs

### Issue: High latency on publish

**Check:**
1. Redis latency: `redis-cli --latency`
2. Network between app and Redis
3. Consider reducing `REDIS_TTL_DAYS` if memory pressure

---

## Why This Configuration for Greenfield?

### Correlation Tracking Enabled by Default

For new deployments, there's **no reason to disable correlation tracking**:

1. **No Migration Risk:** No existing consumers to break
2. **Immediate Value:** Debug capabilities from day 1
3. **Negligible Cost:** 1-2ms latency is insignificant
4. **Future-Proof:** Avoid needing to enable it later
5. **Graceful Degradation:** If Redis fails, publishing continues

### Benefits You Get Day 1

- **Event Lineage:** Track which events caused other events
- **Debugging:** Trace entire event chains through the system
- **Idempotency:** Deterministic event IDs prevent duplicates
- **Observability:** Understand event flow and dependencies

### The Only Reasons to Disable

You might disable correlation tracking only if:
- Running without Redis (not recommended)
- Extreme latency sensitivity (<1ms requirement)
- Temporary testing/development

For production greenfield deployments, **always enable it**.

---

## Next Steps

1. ✅ Deploy with correlation tracking enabled
2. ✅ Verify all health checks pass
3. ✅ Set up monitoring alerts
4. ✅ Test debug endpoints
5. ✅ Document your event schemas
6. ✅ Start publishing events!

---

**Deployment Type:** Greenfield (no legacy systems)
**Recommended Config:** Correlation tracking ENABLED
**Expected Setup Time:** < 30 minutes
**Maintenance:** Minimal (monitor Redis memory)