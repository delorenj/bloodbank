#!/bin/bash
# RabbitMQ Setup Script for Fireflies Transcript Events
# This script sets up the exchanges, queues, and bindings for the event architecture

set -e  # Exit on error

# Configuration
RABBITMQ_HOST="${RABBITMQ_HOST:-localhost}"
RABBITMQ_PORT="${RABBITMQ_PORT:-15672}"  # Management API port
RABBITMQ_USER="${RABBITMQ_USER:-guest}"
RABBITMQ_PASSWORD="${RABBITMQ_PASSWORD:-guest}"
VHOST="${RABBITMQ_VHOST:-/}"

# Exchange and Queue names
EXCHANGE_NAME="fireflies.events"
DLX_NAME="fireflies.events.dlx"
QUEUE_NAME="transcripts.rag.ingestion"
DLQ_NAME="transcripts.failed"
ROUTING_KEY="fireflies.transcript.completed"

echo "=== RabbitMQ Event Architecture Setup ==="
echo "Host: ${RABBITMQ_HOST}:${RABBITMQ_PORT}"
echo "Exchange: ${EXCHANGE_NAME}"
echo "Queue: ${QUEUE_NAME}"
echo ""

# Function to make RabbitMQ API calls
rabbitmq_api() {
    local method=$1
    local path=$2
    local data=$3

    curl -s -u "${RABBITMQ_USER}:${RABBITMQ_PASSWORD}" \
        -H "content-type:application/json" \
        -X "${method}" \
        "http://${RABBITMQ_HOST}:${RABBITMQ_PORT}/api${path}" \
        ${data:+-d "$data"}
}

# 1. Create main exchange for Fireflies events
echo "[1/6] Creating exchange: ${EXCHANGE_NAME}"
rabbitmq_api PUT "/exchanges/${VHOST}/${EXCHANGE_NAME}" \
    '{"type":"topic","durable":true,"auto_delete":false}'

# 2. Create dead-letter exchange
echo "[2/6] Creating dead-letter exchange: ${DLX_NAME}"
rabbitmq_api PUT "/exchanges/${VHOST}/${DLX_NAME}" \
    '{"type":"topic","durable":true,"auto_delete":false}'

# 3. Create dead-letter queue
echo "[3/6] Creating dead-letter queue: ${DLQ_NAME}"
rabbitmq_api PUT "/queues/${VHOST}/${DLQ_NAME}" \
    '{
        "durable":true,
        "auto_delete":false,
        "arguments":{
            "x-message-ttl":604800000
        }
    }'

# 4. Bind dead-letter queue to DLX
echo "[4/6] Binding dead-letter queue to exchange"
rabbitmq_api POST "/bindings/${VHOST}/e/${DLX_NAME}/q/${DLQ_NAME}" \
    '{"routing_key":"transcript.failed"}'

# 5. Create main queue for RAG ingestion
echo "[5/6] Creating queue: ${QUEUE_NAME}"
rabbitmq_api PUT "/queues/${VHOST}/${QUEUE_NAME}" \
    "{
        \"durable\":true,
        \"auto_delete\":false,
        \"arguments\":{
            \"x-message-ttl\":86400000,
            \"x-dead-letter-exchange\":\"${DLX_NAME}\",
            \"x-dead-letter-routing-key\":\"transcript.failed\"
        }
    }"

# 6. Bind main queue to exchange
echo "[6/6] Binding queue to exchange with routing key: ${ROUTING_KEY}"
rabbitmq_api POST "/bindings/${VHOST}/e/${EXCHANGE_NAME}/q/${QUEUE_NAME}" \
    "{\"routing_key\":\"${ROUTING_KEY}\"}"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Exchange topology:"
echo "  ${EXCHANGE_NAME} (topic) → ${QUEUE_NAME}"
echo "  ${DLX_NAME} (topic) → ${DLQ_NAME}"
echo ""
echo "Queue configuration:"
echo "  - Message TTL: 24 hours"
echo "  - Dead-letter after TTL or processing failure"
echo "  - Manual acknowledgment required"
echo ""
echo "You can view the setup at:"
echo "  http://${RABBITMQ_HOST}:${RABBITMQ_PORT}/#/queues"
echo ""
