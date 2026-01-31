"""
Integration tests for RabbitMQ infrastructure validation.

Tests STORY-003 acceptance criteria:
- RabbitMQ running and accessible at configured endpoint
- Exchange created: bloodbank.events.v1
- Test publisher can send events successfully
- Test consumer can receive events successfully
- Latency <100ms for test events
"""

import asyncio
import pytest
import time
from datetime import datetime, timezone
from uuid import uuid4
from typing import List, Dict, Any

from event_producers.rabbit import Publisher
from event_producers.config import settings
from event_producers.events import EventEnvelope, Source, TriggerType, create_envelope
from event_producers.consumer import get_broker

import aio_pika


@pytest.mark.asyncio
async def test_rabbitmq_connection():
    """Test RabbitMQ connection is successful."""
    publisher = Publisher()
    await publisher.start()

    assert publisher._conn is not None
    assert not publisher._conn.is_closed
    assert publisher._channel is not None
    assert not publisher._channel.is_closed
    assert publisher._exchange is not None

    await publisher.close()


@pytest.mark.asyncio
async def test_exchange_creation():
    """Test that the exchange 'bloodbank.events.v1' is created and durable."""
    publisher = Publisher()
    await publisher.start()

    # Verify exchange name
    assert publisher._exchange.name == settings.exchange_name
    assert publisher._exchange.name == "bloodbank.events.v1"

    # Exchange should be durable for persistence
    assert publisher._exchange.durable is True

    await publisher.close()


@pytest.mark.asyncio
async def test_publisher_can_send_events():
    """Test that publisher can successfully send events to RabbitMQ."""
    publisher = Publisher()
    await publisher.start()

    # Create test event
    test_event = {
        "event_type": "test.event.sent",
        "event_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "test_message": "Hello RabbitMQ",
            "test_number": 42
        },
        "source": {
            "host": "test-host",
            "type": "manual",
            "app": "test-suite"
        },
        "version": "1.0.0",
        "correlation_ids": []
    }

    # Publish should not raise exception
    await publisher.publish(
        routing_key="test.event.sent",
        body=test_event,
        event_id=test_event["event_id"]
    )

    await publisher.close()


@pytest.mark.asyncio
async def test_consumer_can_receive_events():
    """Test that consumers can successfully receive events from RabbitMQ."""
    received_events: List[Dict[str, Any]] = []
    test_routing_key = f"test.consumer.receive.{uuid4().hex[:8]}"

    # Create publisher
    publisher = Publisher()
    await publisher.start()

    # Create consumer
    connection = await aio_pika.connect_robust(settings.rabbit_url)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=10)

    # Declare exchange
    exchange = await channel.declare_exchange(
        settings.exchange_name,
        aio_pika.ExchangeType.TOPIC,
        durable=True
    )

    # Create temporary queue
    queue = await channel.declare_queue(
        name=f"test_queue_{uuid4().hex[:8]}",
        durable=False,
        auto_delete=True
    )

    # Bind queue to exchange
    await queue.bind(exchange, routing_key=test_routing_key)

    # Consumer callback
    async def on_message(message: aio_pika.IncomingMessage):
        async with message.process():
            import orjson
            body = orjson.loads(message.body)
            received_events.append(body)

    # Start consuming
    await queue.consume(on_message)

    # Publish test event
    test_event = {
        "event_type": test_routing_key,
        "event_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "test_message": "Consumer test"
        },
        "source": {
            "host": "test-host",
            "type": "manual",
            "app": "test-suite"
        },
        "version": "1.0.0",
        "correlation_ids": []
    }

    await publisher.publish(
        routing_key=test_routing_key,
        body=test_event
    )

    # Wait for message to be received
    for _ in range(50):  # Wait up to 5 seconds
        if received_events:
            break
        await asyncio.sleep(0.1)

    # Verify event was received
    assert len(received_events) == 1
    assert received_events[0]["event_type"] == test_routing_key
    assert received_events[0]["payload"]["test_message"] == "Consumer test"

    # Cleanup - close channel first to stop consuming
    await channel.close()
    await connection.close()
    await publisher.close()


@pytest.mark.asyncio
async def test_event_latency_under_100ms():
    """Test that event publish-to-consume latency is under 100ms."""
    latencies: List[float] = []
    test_routing_key = f"test.latency.{uuid4().hex[:8]}"

    # Create publisher
    publisher = Publisher()
    await publisher.start()

    # Create consumer
    connection = await aio_pika.connect_robust(settings.rabbit_url)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=10)

    exchange = await channel.declare_exchange(
        settings.exchange_name,
        aio_pika.ExchangeType.TOPIC,
        durable=True
    )

    queue = await channel.declare_queue(
        name=f"test_latency_queue_{uuid4().hex[:8]}",
        durable=False,
        auto_delete=True
    )

    await queue.bind(exchange, routing_key=test_routing_key)

    # Consumer callback that measures latency
    async def on_message(message: aio_pika.IncomingMessage):
        async with message.process():
            import orjson
            body = orjson.loads(message.body)
            publish_time = body["payload"]["publish_timestamp"]
            receive_time = time.time()
            latency_ms = (receive_time - publish_time) * 1000
            latencies.append(latency_ms)

    await queue.consume(on_message)

    # Publish 10 test events
    for i in range(10):
        test_event = {
            "event_type": test_routing_key,
            "event_id": str(uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "test_message": f"Latency test {i}",
                "publish_timestamp": time.time()
            },
            "source": {
                "host": "test-host",
                "type": "manual",
                "app": "test-suite"
            },
            "version": "1.0.0",
            "correlation_ids": []
        }

        await publisher.publish(
            routing_key=test_routing_key,
            body=test_event
        )

        # Small delay between messages
        await asyncio.sleep(0.01)

    # Wait for all messages to be received
    for _ in range(100):  # Wait up to 10 seconds
        if len(latencies) >= 10:
            break
        await asyncio.sleep(0.1)

    # Verify latencies
    assert len(latencies) == 10, f"Expected 10 messages, received {len(latencies)}"

    avg_latency = sum(latencies) / len(latencies)
    max_latency = max(latencies)

    print(f"\nLatency statistics:")
    print(f"  Average: {avg_latency:.2f}ms")
    print(f"  Max: {max_latency:.2f}ms")
    print(f"  Min: {min(latencies):.2f}ms")

    # Assert average latency is under 100ms
    assert avg_latency < 100, f"Average latency {avg_latency:.2f}ms exceeds 100ms threshold"

    # Cleanup - close channel first to stop consuming
    await channel.close()
    await connection.close()
    await publisher.close()


@pytest.mark.asyncio
async def test_fanout_to_multiple_consumers():
    """Test that multiple consumers can subscribe to the same event type."""
    consumer1_events: List[Dict[str, Any]] = []
    consumer2_events: List[Dict[str, Any]] = []
    test_routing_key = f"test.fanout.{uuid4().hex[:8]}"

    # Create publisher
    publisher = Publisher()
    await publisher.start()

    # Create two consumers
    conn1 = await aio_pika.connect_robust(settings.rabbit_url)
    channel1 = await conn1.channel()
    exchange1 = await channel1.declare_exchange(
        settings.exchange_name,
        aio_pika.ExchangeType.TOPIC,
        durable=True
    )
    queue1 = await channel1.declare_queue(
        name=f"test_fanout_queue1_{uuid4().hex[:8]}",
        durable=False,
        auto_delete=True
    )
    await queue1.bind(exchange1, routing_key=test_routing_key)

    conn2 = await aio_pika.connect_robust(settings.rabbit_url)
    channel2 = await conn2.channel()
    exchange2 = await channel2.declare_exchange(
        settings.exchange_name,
        aio_pika.ExchangeType.TOPIC,
        durable=True
    )
    queue2 = await channel2.declare_queue(
        name=f"test_fanout_queue2_{uuid4().hex[:8]}",
        durable=False,
        auto_delete=True
    )
    await queue2.bind(exchange2, routing_key=test_routing_key)

    # Consumer callbacks
    async def on_message1(message: aio_pika.IncomingMessage):
        async with message.process():
            import orjson
            body = orjson.loads(message.body)
            consumer1_events.append(body)

    async def on_message2(message: aio_pika.IncomingMessage):
        async with message.process():
            import orjson
            body = orjson.loads(message.body)
            consumer2_events.append(body)

    await queue1.consume(on_message1)
    await queue2.consume(on_message2)

    # Publish test event
    test_event = {
        "event_type": test_routing_key,
        "event_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "test_message": "Fanout test"
        },
        "source": {
            "host": "test-host",
            "type": "manual",
            "app": "test-suite"
        },
        "version": "1.0.0",
        "correlation_ids": []
    }

    await publisher.publish(
        routing_key=test_routing_key,
        body=test_event
    )

    # Wait for messages to be received
    for _ in range(50):
        if consumer1_events and consumer2_events:
            break
        await asyncio.sleep(0.1)

    # Verify both consumers received the event
    assert len(consumer1_events) == 1
    assert len(consumer2_events) == 1
    assert consumer1_events[0]["event_type"] == test_routing_key
    assert consumer2_events[0]["event_type"] == test_routing_key

    # Cleanup - close channels first to stop consuming
    await channel1.close()
    await conn1.close()

    await channel2.close()
    await conn2.close()

    await publisher.close()


@pytest.mark.asyncio
async def test_connection_recovery():
    """Test that publisher can recover from connection issues."""
    publisher = Publisher()
    await publisher.start()

    # Publisher should be started
    assert publisher._started is True

    # Close connection
    await publisher.close()
    assert publisher._started is False

    # Restart connection
    await publisher.start()
    assert publisher._started is True

    # Should be able to publish after restart
    test_event = {
        "event_type": "test.recovery",
        "event_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {"message": "Recovery test"},
        "source": {
            "host": "test-host",
            "type": "manual",
            "app": "test-suite"
        },
        "version": "1.0.0",
        "correlation_ids": []
    }

    await publisher.publish(
        routing_key="test.recovery",
        body=test_event
    )

    await publisher.close()
