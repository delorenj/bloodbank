import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

try:
    from event_producers.events import EventEnvelope, Source, AgentContext
    from rabbit import Publisher
    REAL_PUBLISHER = True
except ImportError:
    print("Dependencies not installed - using mock for demonstration")
    REAL_PUBLISHER = False

from pydantic import BaseModel

class ExamplePayload(BaseModel):
    """A sample event payload for demonstration."""
    message: str
    value: int

class MockPublisher:
    async def start(self): 
        print("MockPublisher started")
    async def publish(self, routing_key: str, body: dict, **kwargs):
        print(f"Mock publish: {routing_key}")
        print(f"Body: {body}")
    async def close(self): 
        print("MockPublisher closed")

async def main():
    """
    This function demonstrates how to create and publish an event using the real system.
    """
    # 1. Initialize the publisher
    publisher = Publisher() if REAL_PUBLISHER else MockPublisher()
    await publisher.start()

    # 2. Create the event payload
    event_payload = ExamplePayload(
        message="Hello from Claude Skill!",
        value=42,
    )

    if REAL_PUBLISHER:
        # 3. Create the event envelope (real system)
        event_envelope = EventEnvelope[ExamplePayload](
            event_type="claude.skill.example.created",
            source=Source(
                component="claude-skill",
                host_id="localhost",
                session_id="example-session"
            ),
            agent_context=AgentContext(
                agent_instance_id="claude-skill-demo",
                task_id="example-task"
            ),
            payload=event_payload,
        )
        body = event_envelope.model_dump(mode="json")
        print(f"Published event: {event_envelope.event_type}")
        print(f"Event ID: {event_envelope.event_id}")
    else:
        # Mock version
        body = {"event_type": "claude.skill.example.created", "payload": event_payload.model_dump()}
        print("Using mock publisher for demonstration")

    # 4. Publish the event
    await publisher.publish(
        routing_key="claude.skill.example.created",
        body=body,
    )

    # 5. Close the publisher connection
    await publisher.close()

if __name__ == "__main__":
    asyncio.run(main())