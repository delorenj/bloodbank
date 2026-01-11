# ADR 0002: Agent Feedback Event Architecture

**Date:** 2026-01-10
**Status:** Proposed
**Deciders:** Architecture Board
**Related:** ADR-0001 (FastStream Adoption)

---

## Context

The AgentForge team proposed integrating mid-session agent feedback capabilities into Bloodbank by introducing `agent.feedback.requested` (command) and `agent.feedback.response` (event) event types. During implementation, several architectural conflicts emerged that expose fundamental misalignments between Bloodbank's current architecture and the proposed integration.

### The Proposals

**AgentForge Integration Team proposed:**
- New agent feedback events in Bloodbank event registry
- `AgentFeedbackRouter` service to consume `agent.feedback.requested` and call AgentForge FastAPI
- AgentForge CLI + FastAPI endpoints for Letta agent management
- Event-first integration maintaining Bloodbank as the event backbone

**Bloodbank Team (FastStream migration) implemented:**
- FastStream-based consumer infrastructure replacing custom `EventConsumer`
- New onboarding docs showing FastStream usage patterns
- Migration path from legacy `EventConsumer` to FastStream

### Core Architectural Conflicts Identified

1. **Envelope Mismatch**: Bloodbank publishes `EventEnvelope` objects, but FastStream onboarding examples treat handlers as receiving raw payloads
2. **Legacy Consumer Dependency**: Existing services still import deprecated `EventConsumer`, will break when `event_producers` no longer exports it
3. **Command Processor Conflict**: `AgentFeedbackRequested` extends `BaseCommand` with `NotImplementedError` in `execute()`, causing runtime failures when command processor auto-registers it
4. **Convention Drift**: FastStream examples don't follow AGENTS.md queue naming conventions

---

## Core Problem Statement

**The fundamental question:** Should Bloodbank be responsible for consuming agent events that originate from AgentForge?

This question reveals a deeper architectural tension:
- **Who owns agent orchestration?** AgentForge or a future Flume service?
- **What is Bloodbank's role?** Pure message bus or message bus + selective command execution?
- **How do we model feedback requests?** As commands (imperative, invokable) or events (declarative, reactive)?

---

## Decision

### Primary Decision: Bloodbank is a Pure Event Backbone

**Bloodbank SHALL NOT consume and execute business logic for agent events.**

Bloodbank's responsibility is **event transport and correlation tracking**, not **agent orchestration**. The following architectural principles apply:

1. **Separation of Concerns**
   - Bloodbank: Event pub/sub infrastructure with correlation tracking
   - AgentForge: Agent runtime and invocation interface
   - Flume (future): Hierarchical orchestration and routing logic

2. **Event vs Command Distinction**
   - Events (Facts): Declarative notifications of things that happened
   - Commands: Should NOT be consumed by Bloodbank's command processor unless Bloodbank owns the execution domain

3. **Consumer Independence**
   - Services that need to react to events consume directly from Bloodbank
   - Services don't depend on Bloodbank to execute their business logic

### Secondary Decision: Resolve Command Processor Conflict

**`AgentFeedbackRequested` SHALL BE refactored as a pure event**, not a `BaseCommand`:

```python
# BEFORE (Incorrect)
class AgentFeedbackRequested(BaseCommand[AgentFeedbackResponse]):
    async def execute(...):
        raise NotImplementedError(...)  # ❌ Runtime failure

# AFTER (Correct)
class AgentFeedbackRequested(BaseEvent):
    # No execute() method - this is a fact, not a command
    agent_id: str
    message: str
    ...
```

**Rationale:**
- Bloodbank's command processor auto-discovers `Invokable` subclasses
- If `AgentFeedbackRequested` is `Invokable` but always raises `NotImplementedError`, the system fails by design
- Agent feedback execution is AgentForge's domain, not Bloodbank's

### Tertiary Decision: Standardize Envelope Handling

**All FastStream consumers SHALL unwrap `EventEnvelope` explicitly**:

```python
from event_producers.events.base import EventEnvelope
from event_producers.events.domains.agent.feedback import AgentFeedbackRequested

@broker.subscriber(
    queue="services.agentforge.feedback_router",
    exchange="bloodbank.events.v1",
    routing_key="agent.feedback.requested"
)
async def handle_feedback_request(envelope: EventEnvelope):
    # Unwrap envelope to get payload
    payload = AgentFeedbackRequested(**envelope.payload)

    # Use correlation metadata
    correlation_ids = envelope.correlation_ids

    # Execute business logic
    response = await call_agentforge_api(payload, correlation_ids)
```

**Rationale:**
- Preserves Bloodbank's envelope schema discipline
- Enables correlation tracking across service boundaries
- Maintains backward compatibility with existing publishers

---

## Consequences

### Positive

1. **Clear Ownership**: AgentForge owns agent execution, Bloodbank owns event transport
2. **Modularity**: Services can evolve independently without tight coupling through Bloodbank
3. **Future-Proof**: Flume can add orchestration logic without rewriting Bloodbank primitives
4. **Reduced Regression Risk**: Bloodbank command processor only executes commands it owns
5. **Correlation Integrity**: EventEnvelope remains the canonical wire format for correlation tracking

### Negative

1. **Service Discovery**: Consumers need to know which events to subscribe to (not auto-routed by command processor)
2. **Boilerplate**: Each consumer must explicitly unwrap EventEnvelope
3. **Documentation Burden**: Need clear examples showing envelope unwrapping in FastStream

### Neutral

1. **AgentFeedbackRouter becomes a standalone service** (not embedded in Bloodbank)
2. **FastStream migration requires updating all consumers** to handle EventEnvelope explicitly
3. **Queue naming conventions must be enforced** through documentation and review

---

## Implementation Plan

### Phase 1: Fix Command Processor Conflict (Immediate)

1. Refactor `AgentFeedbackRequested` from `BaseCommand` to `BaseEvent`
2. Remove `execute()` method implementation
3. Update event registry to reflect change
4. Add unit test verifying command processor doesn't auto-register it

**Owner:** Bloodbank Team
**Effort:** XS (1-2 hours)

### Phase 2: Create AgentFeedbackRouter Service (Short-term)

1. Create standalone service at `services/agent_feedback_router/`
2. Implement FastStream consumer for `agent.feedback.requested`
3. Call AgentForge FastAPI endpoint
4. Publish `agent.feedback.response` with correlation IDs
5. Add integration tests

**Owner:** AgentForge Team
**Effort:** S (4-8 hours)

### Phase 3: Standardize FastStream Envelope Handling (Medium-term)

1. Update `docs/ONBOARDING_FASTSTREAM.md` with EventEnvelope examples
2. Create FastStream dependency injection helper for envelope unwrapping:
   ```python
   from event_producers.events.faststream import unwrap_envelope

   @broker.subscriber(...)
   async def handler(payload: AgentFeedbackRequested = Depends(unwrap_envelope)):
       # payload is already unwrapped and validated
       ...
   ```
3. Migrate existing consumers (Fireflies, etc.) to envelope-aware pattern
4. Add compatibility shim if needed for gradual migration

**Owner:** Bloodbank Team
**Effort:** M (1-2 days)

### Phase 4: Enforce Queue Naming Convention (Ongoing)

1. Document queue naming standard: `services.<domain>.<service_name>`
2. Add pre-commit hook to validate queue names in code
3. Update all existing queues to follow convention
4. Add linter rule for FastStream decorator params

**Owner:** DevOps + Bloodbank Team
**Effort:** S (4-6 hours)

---

## Architectural Patterns Established

### Pattern 1: Event Ownership

**Rule:** The service that publishes an event owns its schema and lifecycle. Bloodbank is the transport, not the owner.

**Example:**
- `fireflies.transcript.ready` → Owned by Fireflies webhook processor
- `agent.feedback.requested` → Owned by requesting service (could be Flume, n8n workflow, etc.)
- `agent.feedback.response` → Owned by AgentForge

### Pattern 2: Command Processor Scope

**Rule:** Bloodbank's command processor ONLY executes commands where Bloodbank owns the execution domain.

**Examples of valid Bloodbank commands:**
- `bloodbank.correlation.rebuild` - Rebuild correlation graph from events
- `bloodbank.event.replay` - Replay events from archive
- `bloodbank.registry.refresh` - Refresh event registry from filesystem

**Examples of INVALID Bloodbank commands:**
- `agent.feedback.requested` - AgentForge owns execution
- `llm.prompt` - LLM service owns execution
- `fireflies.transcript.upload` - Fireflies service owns execution

### Pattern 3: Consumer Service Architecture

**Rule:** Services consuming events from Bloodbank are independent microservices, not embedded in Bloodbank.

**Directory Structure:**
```
bloodbank/
├── event_producers/          # Core Bloodbank infrastructure
│   ├── events/               # Event schemas and registry
│   ├── rabbit.py             # Publisher
│   └── command_processor.py  # Bloodbank-owned commands only
└── services/                  # Independent consumer services
    ├── agent_feedback_router/ # AgentForge consumer
    ├── fireflies_processor/   # Fireflies RAG ingestion
    └── notification_service/  # Cross-domain notifications
```

### Pattern 4: EventEnvelope as Wire Format

**Rule:** All events published to Bloodbank MUST be wrapped in `EventEnvelope`. All consumers MUST unwrap it.

**Publisher Side:**
```python
envelope = create_envelope(
    event_type="agent.feedback.requested",
    payload=AgentFeedbackRequested(...),
    source=create_source(...),
    correlation_ids=[parent_event_id]
)
await publisher.publish("agent.feedback.requested", envelope.model_dump())
```

**Consumer Side (FastStream):**
```python
@broker.subscriber(...)
async def handle(envelope_dict: dict):
    envelope = EventEnvelope(**envelope_dict)
    payload = AgentFeedbackRequested(**envelope.payload)
    # Use envelope.correlation_ids, envelope.source, etc.
```

---

## Trade-offs Analysis

### Option A: Bloodbank Consumes Agent Events (Rejected)

**Pros:**
- Centralized command execution
- Single consumer codebase
- Automatic routing via command processor

**Cons:**
- Tight coupling between Bloodbank and AgentForge
- Bloodbank becomes bloated with domain logic
- Violates single responsibility principle
- Harder to scale services independently
- Command processor becomes a God object

### Option B: Services Consume Directly (Accepted)

**Pros:**
- Clear service boundaries
- Independent scaling
- Domain ownership
- Modular evolution
- Testable in isolation

**Cons:**
- More services to deploy
- Slight duplication of consumer setup boilerplate
- Services must discover which events to subscribe to

**Decision:** Option B aligns with 33GOD principles of modularity, clear ownership, and layered abstraction.

---

## Validation Criteria

This decision is successful if:

1. ✅ `AgentFeedbackRequested` does not cause runtime failures in command processor
2. ✅ AgentFeedbackRouter service can be deployed independently of Bloodbank
3. ✅ Correlation tracking works end-to-end from request → AgentForge → response
4. ✅ FastStream consumers can unwrap EventEnvelope without breaking changes
5. ✅ No Bloodbank code contains AgentForge-specific business logic
6. ✅ Future Flume integration can add routing logic without modifying Bloodbank

---

## References

### Related Documents
- [Bloodbank Architecture](/home/delorenj/code/33GOD/bloodbank/trunk-main/docs/ARCHITECTURE.md)
- [ADR-0001: Adopt FastStream](/home/delorenj/d/Projects/33GOD/ARB/bloodbank/0001-faststream/0001-adopt-faststream.md)
- [AgentForge Integration Brief](/home/delorenj/d/Projects/33GOD/ARB/bloodbank/0001-faststream/AgentForgeIntegrationTeam.md)

### Key Code Locations
- Command abstraction: `/event_producers/events/core/abstraction.py`
- Command processor: `/event_producers/command_processor.py`
- Agent feedback events: `/event_producers/events/domains/agent/feedback.py`
- FastStream onboarding: `/docs/ONBOARDING_FASTSTREAM.md`

### Design Principles Referenced
- Single Responsibility Principle
- Separation of Concerns
- Domain-Driven Design (Bounded Contexts)
- Event-Driven Architecture (Event vs Command distinction)
- 33GOD Ecosystem Patterns (Modular microservices, clear ownership)

---

## Approval

**Recommended for approval by:** Architecture Board
**Next steps:** Phase 1 implementation (fix command processor conflict)
**Follow-up review:** After Phase 2 completion (AgentFeedbackRouter deployed)
