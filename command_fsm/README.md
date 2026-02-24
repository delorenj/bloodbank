# Agent Command FSM

Redis-backed finite state machine for agent command processing with atomic CAS transitions.

## Components

- **FSMManager**: High-level API for command acceptance and state transitions
- **RedisAgentFSMStore**: Redis storage layer with Lua CAS scripts
- **IdempotencyStore**: Idempotency key tracking (300s dedup window)
- **FSMState**: State enum (idle, acknowledging, working, blocked, error, paused)
- **StateTransition**: Transition rules with guards and side effects

## Usage

```python
from command_fsm import FSMManager, FSMState, CommandGuardResult
from datetime import datetime, timezone
from uuid import uuid4

manager = FSMManager(redis_url="redis://localhost:6379")

# Accept a command
result, state = manager.accept_command(
    agent_name="lenoon",
    command_id=uuid4(),
    issued_at=datetime.now(timezone.utc),
    ttl_ms=30000,
    idempotency_key="drift-check-2026-02-24"
)

if result == CommandGuardResult.PASSED:
    # Publish ack event
    print(f"Command accepted, FSM version: {state.version}")
    
    # Mark working
    success, state = manager.mark_working("lenoon")
    
    # ... execute command ...
    
    # Mark completed
    success, state = manager.mark_completed("lenoon")
    print(f"Command completed, back to idle (v{state.version})")
```

## State Transitions

```
idle → acknowledging → working → idle (success)
                       ├──────→ error (failure)
                       └──────→ blocked → working
                                      └→ error (timeout)

Any state → paused → {previous state} (resume)
```

## Redis Schema

### FSM State
```
Key:    agent:{name}:fsm
Type:   HASH
Fields:
  state           = FSMState enum value
  version         = integer (monotonic, for optimistic concurrency)
  command_id      = uuid (current command, null if idle)
  entered_at      = ISO 8601 timestamp
  pre_pause_state = state before pause (null if not paused)
  ttl_deadline    = ISO 8601 timestamp (command expiry)
```

### Idempotency Keys
```
Key:  agent:{name}:idemp:{idempotency_key}
Type: STRING (value = command_id)
TTL:  300s (5 minute dedup window)
```

## Guards

Commands are rejected if:
- Agent is paused (`CommandGuardResult.PAUSED`)
- Agent not in idle state (`CommandGuardResult.INVALID_STATE`)
- TTL expired (`CommandGuardResult.EXPIRED`)
- Idempotency key seen before (`CommandGuardResult.DUPLICATE`)

## CAS Retries

The FSM uses optimistic concurrency control (version numbers) for atomic transitions.
If a version conflict occurs (concurrent modification), the operation retries up to 3 times
with the updated version before failing with `CommandGuardResult.VERSION_CONFLICT`.

## Testing

**Unit tests** (no Redis required):
```bash
mise exec -- pytest tests/command_fsm/test_states.py -v
```

**Integration tests** (requires real Redis):
```bash
# Start Redis
docker run -d -p 6379:6379 redis:7-alpine

# Run FSM manager tests
mise exec -- pytest tests/command_fsm/test_fsm_manager.py -v
```

Note: fakeredis does not support Lua scripts (`EVALSHA`), so FSM manager tests require a real Redis instance.

## Monitoring

Key metrics to track:
- CAS retry rate (should be low, <5% of transitions)
- Version conflict rate
- Command guard rejection breakdown (expired vs invalid_state vs duplicate)
- Average time in each state
- Orphaned commands (commands that never complete)

## Recovery

Force reset an agent to idle:
```python
manager.reset("agent_name")  # Bypasses CAS, always succeeds
```

Manual idempotency key removal:
```python
manager.idemp_store.remove("agent_name", "some-key")
```
