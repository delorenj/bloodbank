---
modified: 2025-12-18T08:34:04-05:00
---
# Event-Driven Architecture Documentation

## Project Information

- Current phase: `Design core command/event patterns`

---
## Architecture Overview

### Component Control Flow

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│                 │     │                 │     │                 │
│  External       │     │  SQS Queue      │     │  Transaction    │
│  System         │─────┤                 │─────┤  Listener       │
│                 │     │                 │     │                 │
└─────────────────┘     └─────────────────┘     └────────┬────────┘
                                                         │
                                                         ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│                 │     │                 │     │                 │
│  Event          │     │  Command        │     │  Transaction    │
│  Emitter        │◄────┤  Execution      │◄────┤  Manager        │
│                 │     │                 │     │                 │
└─────────────────┘     └─────────────────┘     └────────┬────────┘
                                                         │
                                                         ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│                 │     │                 │     │                 │
│  Downstream     │     │  SQS Queue      │     │  Side Effect    │
│  Systems        │◄────┤  (Side Effects) │◄────┤  Events         │
│                 │     │                 │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

---

## The Command API

### Frontend Event Triggering Flow

The frontend calls a thin API layer that immediately acknowledges the command reception, emits the event, and returns a correlation ID. The frontend can then poll/subscribe for updates using this ID.

**Benefits:**

- Immediate acknowledgment to users
- Maintains event-driven purity in backend
- Provides tracking mechanism for frontend

### Detailed Walkthrough

#### 1. The Immediate Handshake

When the user clicks "Create Order", the frontend makes a call to our Command API endpoint that:

- Generates a `correlationId` (e.g., `xyz123`) to track the command through its lifecycle
- Performs basic validation for fail-fast on malformed requests
- Returns immediate acknowledgment to the frontend

#### 2. The Event Translation

The Command API translates between synchronous web expectations and asynchronous event architecture, transforming the HTTP request into a proper domain event with the `correlationId` baked in.

#### 3. The Status Channel

The frontend maintains a "digital tether" to the process via polling (MVP approach). This provides continuous feedback without forcing synchronous stack behavior.

#### 4. The Transaction Flow

The event-driven architecture takes over:

- `TransactionListener` picks up the event
- `TransactionManager` orchestrates command execution, handling retries and timeouts
- `Command` handles business logic and database operations
- Resulting `OrderCreatedEvent` flows back to the event bus

#### 5. The Correlation Resolution

The frontend eventually sees the `OrderCreatedEvent` with the original `correlationId`, completing the circuit and transitioning UI to confirmed state.

---

## Command Manager and Execution Flow

### Core Component Interactions

#### `TransactionListener` → `TransactionManager`

- Acts as entry point for events
- Forwards events to manager
- Contains no business logic, only routing and basic validation

#### `TransactionManager` → `TransactionCommandBuilder`

- Extracts event type and context
- Requests appropriate command from builder
- Passes context information needed for command construction

#### `TransactionCommandBuilder` → `TransactionCommand`

- Creates appropriate command instance based on event type
- Injects dependencies into the command
- Sets up command with event data and context

#### `TransactionManager` → `TransactionCommand`

- Manages command's lifecycle
- Invokes command's execution
- Handles results and determines next steps

#### `TransactionCommand` → `EventCollector`

- During execution, command enqueues side effect events
- These represent intended consequences of the command

#### `TransactionManager` → `EventCollector` & `EventEmitter`

- After successful command execution, manager retrieves collected events
- Publishes these events through the emitter

#### `TransactionManager` → `ErrorHandler`

- On execution failure, delegates to error handler
- Receives recovery strategy decisions (retry, fail)

### SQL Transaction Integrity

**Transactional Outbox Pattern:**

- Side effects are first written to an outbox table in the same transaction
- A separate process processes the outbox and emits the awaiting side effect
- This way side effects are bound to the SQL update and published as one atomic operation

**MVP Implementation:** The simplest SAFE implementation. Known limitation:

> If there is a system failure AFTER database mutations are committed but BEFORE emitting side effects, the side effect will NOT be emitted.

This is acceptable for MVP.

---

## Command / Executor Pattern

### Components

- **Event Listener:** Entry point for events, provides event handling registration
- **Command Manager:** Central orchestrator coordinating entire event/command lifecycle
- **Command Builder:** Factory constructing appropriate Command objects based on event types
- **Invokable:** Interface defining contract for all command implementations
- **Concrete Command:** Actual implementation containing business logic
- **Event Collector:** Aggregates side effects produced during command execution
- **Error Handler:** Provides resilient processing with retry capabilities
- **Event Emitter:** Publishes events after transaction completion
