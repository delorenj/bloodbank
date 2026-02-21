#!/bin/bash
# Debug script for Bloodbank RabbitMQ Publisher
# Created: 2026-02-20 by Grolf during live events verification

echo "=== Bloodbank Publisher Debug ==="
echo ""

echo "1. Checking RABBIT_URL environment variable:"
docker exec 33god-bloodbank env | grep RABBIT_URL
echo ""

echo "2. Checking Publisher startup logs:"
docker logs 33god-bloodbank 2>&1 | grep -i "publisher\|rabbitmq\|amqp" | head -20
echo ""

echo "3. Checking RabbitMQ connection from inside container:"
docker exec 33god-bloodbank python3 -c "
import asyncio
import aio_pika
import os

async def test_connection():
    try:
        url = os.getenv('RABBIT_URL', 'amqp://guest:guest@localhost:5672/')
        print(f'Attempting connection to: {url}')
        connection = await aio_pika.connect_robust(url)
        print('✅ Connection successful!')
        channel = await connection.channel()
        print('✅ Channel created!')
        exchange = await channel.declare_exchange('bloodbank.events.v1', aio_pika.ExchangeType.TOPIC, durable=True)
        print(f'✅ Exchange declared: {exchange.name}')
        await connection.close()
    except Exception as e:
        print(f'❌ Connection failed: {e}')

asyncio.run(test_connection())
" 2>&1
echo ""

echo "4. Checking RabbitMQ queues:"
docker exec theboard-rabbitmq rabbitmqctl list_queues name messages consumers | grep -E "blood|amq_"
echo ""

echo "5. Publishing test event:"
RESPONSE=$(docker exec 33god-bloodbank curl -s -X POST http://localhost:8682/events/agent/debug-test/heartbeat \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "debug-test", "status": "ok"}')
echo "$RESPONSE"
echo ""

echo "6. Checking queue messages after publish (should increment):"
sleep 1
docker exec theboard-rabbitmq rabbitmqctl list_queues name messages | grep -E "blood|amq_"
echo ""

echo "=== Debug Complete ==="
