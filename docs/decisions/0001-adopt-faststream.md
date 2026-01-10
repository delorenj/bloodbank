# ADR 0001: Adopt FastStream for Event Consumption

**Date:** 2026-01-10
**Status:** Accepted

## Context

Bloodbank requires a robust mechanism for consuming events from RabbitMQ. Initially, a custom `EventConsumer` class was implemented using `aio-pika` directly. This implementation used an inheritance-based pattern where specific consumers subclassed `EventConsumer` and methods were auto-registered via `__init_subclass__`.

During code review, critical issues were identified in this custom implementation:
1.  **Shared State:** The handler registry was a class attribute on the base class, causing all subclasses to share the same handlers (collisions).
2.  **Poison Pill Vulnerability:** Malformed JSON caused an infinite retry loop because exceptions weren't caught/rejected properly.
3.  **Connection Leaks:** The underlying connection wasn't exposed for clean shutdown.
4.  **Testing Difficulty:** The tight coupling between the class structure and the connection logic made unit testing handlers difficult.

## Decision

We have decided to replace the custom `EventConsumer` infrastructure with **FastStream**.

[FastStream](https://faststream.airt.ai/) is a modern Python framework for building event-driven microservices. It abstracts `aio-pika` and integrates seamlessly with Pydantic for data validation.

## Consequences

### Positive
*   **Safety:** Eliminates the shared state bug found in the custom implementation.
*   **Productivity:** Reduces boilerplate code. Developers focus on business logic functions rather than class infrastructure.
*   **Reliability:** FastStream handles reconnection, retries, and error handling (including poison pill rejection) out of the box.
*   **Type Safety:** Native Pydantic integration ensures that payloads are validated before reaching the handler.
*   **Composition:** Favors composition (decorators) over inheritance.
*   **Documentation:** Supports AsyncAPI schema generation.

### Negative
*   **Dependency:** Introduces a new external dependency (`faststream[rabbit]`).
*   **Learning Curve:** Team needs to learn the FastStream API (though it is similar to FastAPI).

## Implementation

*   The `event_producers/consumer.py` file has been refactored to export a configured `RabbitBroker` and `FastStream` app instance.
*   The `pyproject.toml` has been updated to include the dependency.
*   Documentation has been updated to reflect the new pattern.
