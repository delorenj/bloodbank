import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from event_producers.events import EventEnvelope, Source, AgentContext
from rabbit import Publisher
from pydantic import BaseModel

class ExamplePayload(BaseModel):
    """A sample event payload for demonstration."""
    message: str
    value: int

async def main():
    """
    This function demonstrates how to create and publish an event using the real system.
    """
    # 1. Initialize the publisher
    publisher = Publisher()
    await publisher.start()

    # 2. Create the event payload
    event_payload = ExamplePayload(
        message="Hello from Claude Skill!",
        value=42,
    )

    # 3. Create the event envelope
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

    # 4. Publish the event
    await publisher.publish(
        routing_key="claude.skill.example.created",
        body=event_envelope.model_dump(mode="json"),
    )

    print(f"Published event: {event_envelope.event_type}")
    print(f"Event ID: {event_envelope.event_id}")

    # 5. Close the publisher connection
    await publisher.close()

if __name__ == "__main__":
    asyncio.run(main())