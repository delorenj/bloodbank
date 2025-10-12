#!/usr/bin/env python3
"""
Bloodbank RabbitMQ Test Script
Quick smoke test for your RabbitMQ → n8n integration
"""

import sys
import json
import time
from datetime import datetime

try:
    import pika
except ImportError:
    print("❌ pika not installed. Run: pip install pika")
    sys.exit(1)


def test_connection(url):
    """Test basic connectivity"""
    print("🔌 Testing connection...")
    try:
        conn = pika.BlockingConnection(pika.URLParameters(url))
        ch = conn.channel()
        print("✅ Connected to RabbitMQ")
        return ch
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)


def declare_exchange(ch, exchange_name):
    """Ensure exchange exists"""
    print(f"\n📢 Declaring exchange: {exchange_name}")
    try:
        ch.exchange_declare(
            exchange=exchange_name,
            exchange_type="topic",
            durable=True
        )
        print(f"✅ Exchange '{exchange_name}' ready")
    except Exception as e:
        print(f"❌ Exchange declaration failed: {e}")
        sys.exit(1)


def publish_test_events(ch, exchange_name):
    """Publish a variety of test events"""
    events = [
        {
            "routing_key": "llm.prompt",
            "data": {
                "event_type": "llm_interaction",
                "prompt": "Test prompt from CLI",
                "timestamp": datetime.utcnow().isoformat(),
                "source": "test_script"
            }
        },
        {
            "routing_key": "llm.response",
            "data": {
                "event_type": "llm_interaction",
                "response": "Test response from CLI",
                "timestamp": datetime.utcnow().isoformat(),
                "source": "test_script"
            }
        },
        {
            "routing_key": "artifact.created",
            "data": {
                "event_type": "artifact",
                "artifact_id": "test-artifact-001",
                "type": "markdown",
                "timestamp": datetime.utcnow().isoformat(),
                "source": "test_script"
            }
        },
        {
            "routing_key": "webhook.test",
            "data": {
                "event_type": "webhook",
                "webhook_name": "test",
                "payload": {"test": "data"},
                "timestamp": datetime.utcnow().isoformat(),
                "source": "test_script"
            }
        }
    ]

    print(f"\n📨 Publishing {len(events)} test events...")
    for event in events:
        try:
            ch.basic_publish(
                exchange=exchange_name,
                routing_key=event["routing_key"],
                body=json.dumps(event["data"]),
                properties=pika.BasicProperties(
                    delivery_mode=2,  # persistent
                    content_type="application/json"
                )
            )
            print(f"  ✅ Published: {event['routing_key']}")
            time.sleep(0.1)  # Small delay between publishes
        except Exception as e:
            print(f"  ❌ Failed to publish {event['routing_key']}: {e}")

    print("\n✨ All events published!")


def main():
    """Main test flow"""
    print("=" * 60)
    print("🩸 Bloodbank RabbitMQ Test Script")
    print("=" * 60)

    # Configuration - will prompt for actual credentials
    print(f"\n📋 Configuration:")
    print(f"   Exchange: bloodbank.events.v1")

    # Get credentials from user
    print("\n🔑 Enter RabbitMQ credentials:")
    host = input("   Host (localhost or bloodbank.messaging.svc) [localhost]: ").strip() or "localhost"
    port = input("   Port (5673 for port-forward, 5672 for in-cluster) [5673]: ").strip() or "5673"
    user = input("   Username: ").strip()
    password = input("   Password: ").strip()
    
    RABBIT_URL = f"amqp://{user}:{password}@{host}:{port}/"
    EXCHANGE_NAME = "bloodbank.events.v1"

    # Run tests
    ch = test_connection(RABBIT_URL)
    declare_exchange(ch, EXCHANGE_NAME)
    publish_test_events(ch, EXCHANGE_NAME)

    print("\n" + "=" * 60)
    print("✅ Test complete!")
    print("\n📝 Next steps:")
    print("   1. Open RabbitMQ UI: http://localhost:15672")
    print("   2. Check Exchanges → bloodbank.events.v1 → bindings")
    print("   3. Check your n8n workflow executions")
    print("   4. Look for your test events in the consumer logs")
    print("=" * 60)

    ch.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Interrupted by user")
        sys.exit(0)
