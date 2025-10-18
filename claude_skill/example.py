import asyncio
import uuid
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from typing import Optional

# This is a simplified, self-contained example that mirrors the structure
# of the event system. In a real-world scenario, you would import the
# Publisher class and event models from the existing modules.

# --- Mock Objects for Demonstration ---

class MockPublisher:
    """A mock publisher to simulate the real Publisher class."""
    async def publish(self, routing_key: str, body: dict, **kwargs):
        print(f"Publishing event with routing key '{routing_key}':")
        print(f"  Body: {body}")
        print("-" * 20)

    async def start(self):
        print("MockPublisher started.")

    async def close(self):
        print("MockPublisher closed.")

# --- Event Definitions (as in event_producers/events.py) ---

class Source(BaseModel):
    component: str
    host_id: str
    session_id: Optional[str] = None

class EventEnvelope(BaseModel):
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0.0"
    source: Source
    payload: BaseModel

class ExamplePayload(BaseModel):
    """A sample event payload for demonstration."""
    message: str
    value: int

# --- Main Publishing Logic ---

async def main():
    """
    This function demonstrates how to create and publish an event.
    """
    # 1. Initialize the publisher
    publisher = MockPublisher()
    await publisher.start()

    # 2. Create the event payload
    event_payload = ExamplePayload(
        message="Hello, Event Bus!",
        value=42,
    )

    # 3. Create the event envelope
    event_envelope = EventEnvelope(
        event_type="example.event.created",
        source=Source(
            component="claude-skill-example",
            host_id="localhost",
        ),
        payload=event_payload,
    )

    # 4. Publish the event
    await publisher.publish(
        routing_key="example.event.created",
        body=event_envelope.model_dump(mode="json"),
    )

    # 5. Close the publisher connection
    await publisher.close()

if __name__ == "__main__":
    asyncio.run(main())