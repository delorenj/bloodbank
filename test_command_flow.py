import asyncio
import logging
from uuid import uuid4

# Setup basic logging
logging.basicConfig(level=logging.INFO)

from event_producers.events.core.manager import CommandManager
from event_producers.events.core.factory import get_command_factory
from event_producers.events.domains.agent.commands import ProcessAgentPromptCommand
from event_producers.events.domains.agent.thread import AgentThreadPrompt
from event_producers.events.base import create_envelope, Source, TriggerType
from event_producers.rabbit import Publisher

# Mock Publisher for testing without RabbitMQ connection
class MockPublisher(Publisher):
    def __init__(self):
        self.published_messages = []

    async def publish(self, routing_key, payload, message_id=None, correlation_id=None):
        print(f" [MockBus] Published to '{routing_key}': {payload}")
        self.published_messages.append((routing_key, payload))

async def test_flow():
    # 1. Setup
    publisher = MockPublisher()
    manager = CommandManager(publisher)
    factory = get_command_factory()

    # 2. Register Command
    # In a real app, this registration would happen at startup via discovery or manual config
    factory.register("agent.thread.prompt", ProcessAgentPromptCommand)

    # 3. Create initial Event (The Trigger)
    payload = AgentThreadPrompt(
        provider="test-provider",
        prompt="Hello Command Pattern!",
        project="bloodbank"
    )
    
    source = Source(host="test-host", type=TriggerType.MANUAL, app="test-script")
    
    envelope = create_envelope(
        event_type="agent.thread.prompt",
        payload=payload.model_dump(),
        source=source,
        event_id=uuid4()
    )

    print(f" [Test] Handling envelope: {envelope.event_type}")

    # 4. Process via Manager
    await manager.handle_envelope(envelope)

    # 5. Assertions
    assert len(publisher.published_messages) == 1
    key, msg = publisher.published_messages[0]
    assert key == "agent.thread.response"
    assert "Echoing your prompt" in str(msg)
    print(" [Test] Success! Side effect published.")

if __name__ == "__main__":
    asyncio.run(test_flow())
