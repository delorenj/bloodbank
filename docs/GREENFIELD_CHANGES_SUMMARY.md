# Greenfield Deployment Changes - Summary

## What Changed

### 1. Created Simplified Deployment Guide
**File:** `GREENFIELD_DEPLOYMENT.md`
- Single, straightforward deployment checklist
- Removed all phased rollout strategies
- Removed backward compatibility migration steps
- Removed "enable correlation tracking gradually" advice
- Removed time-based milestones
- Binary choice: Enable correlation tracking (YES for greenfield)

### 2. Enabled Correlation Tracking by Default
**Files Updated:**
- `/event_producers/http.py`
- `/event_producers/mcp_server.py`

**Change:**
```python
# Before (v1 default):
publisher = Publisher()  # Correlation tracking disabled

# After (greenfield default):
publisher = Publisher(enable_correlation_tracking=True)  # Enabled by default
```

### 3. Updated README
**File:** `README.md`
- Added Quick Start section
- Links to GREENFIELD_DEPLOYMENT.md for new deployments
- Links to MIGRATION_v1_to_v2.md for migrations

---

## Justification for Enabling Correlation Tracking by Default

For **greenfield deployments**, correlation tracking should be enabled by default because:

### ✅ No Migration Risk
- No existing consumers to break
- No backward compatibility concerns
- Clean slate means optimal configuration from day 1

### ✅ Negligible Performance Impact
- Only adds 1-2ms latency per publish
- Circuit breaker prevents blocking (1s timeout)
- Graceful degradation if Redis unavailable

### ✅ Immediate Debugging Value
- Event lineage tracking from day 1
- Correlation chains for troubleshooting
- Debug endpoints available immediately
- No need to "enable later when you need it"

### ✅ Deterministic Event IDs
- Idempotency built-in from the start
- Prevents duplicate event processing
- UUID v5 based on routing key + attributes

### ✅ Future-Proof
- Avoids technical debt of enabling later
- No disruption to enable debugging capabilities
- Better observability from the beginning

### ❌ The Only Reasons to Disable

You would only disable correlation tracking if:
1. **No Redis Available** - Not recommended for production
2. **Extreme Latency Requirements** - Need <1ms publish time
3. **Temporary Testing** - Quick local development without Redis

For production greenfield deployments, there's **no valid reason** to disable it.

---

## Simplified Deployment Process

### Before (Phased Approach)
1. Deploy with correlation disabled
2. Monitor for 1-2 days
3. Set up Redis monitoring
4. Enable on one service
5. Gradual rollout over 3-5 days
6. Migrate consumers over 1-2 weeks
**Total: 2-3 weeks**

### After (Greenfield Approach)
1. Install dependencies
2. Configure Redis & RabbitMQ
3. Deploy with correlation enabled
4. Verify health checks
**Total: < 30 minutes**

---

## Files Changed

| File | Change | Purpose |
|------|--------|---------|
| `GREENFIELD_DEPLOYMENT.md` | Created | Simplified deployment guide |
| `event_producers/http.py` | Enable correlation by default | Better debugging from day 1 |
| `event_producers/mcp_server.py` | Enable correlation by default | Consistency with http.py |
| `README.md` | Add Quick Start section | Point to appropriate guide |
| `GREENFIELD_CHANGES_SUMMARY.md` | Created | This summary |

---

## Verification

After deployment, verify correlation tracking is working:

```bash
# 1. Publish a test event
curl -X POST http://localhost:8682/events/llm/prompt \
  -H "Content-Type: application/json" \
  -d '{"prompt": "test", "model": "gpt-4", "temperature": 0.7}'

# 2. Get the event_id from response
# 3. Check correlation data
curl http://localhost:8682/debug/correlation/{event_id}
```

If Redis is not available, events will still publish but without correlation tracking (graceful degradation).