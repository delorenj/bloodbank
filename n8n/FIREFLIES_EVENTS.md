# Fireflies Transcript Event Architecture

Event-driven architecture for processing Fireflies transcriptions and ingesting them into a RAG system using RabbitMQ.

## Architecture Overview

```
Fireflies Webhook → n8n Workflow → RabbitMQ (Durable Events) → RAG Consumer
```

### Event Flow

1. **Fireflies** sends webhook when transcription completes
2. **n8n workflow** fetches full transcript via Fireflies API
3. **Transform node** creates standardized event payload
4. **RabbitMQ** publishes durable event to topic exchange
5. **RAG Consumer** processes events and ingests into RAG system

## Components

### 1. RabbitMQ Event Schema (`rabbitmq-event-schema.json`)

Defines the complete event architecture:

- **Exchange**: `fireflies.events` (topic, durable)
- **Routing Key**: `fireflies.transcript.completed`
- **Queue**: `transcripts.rag.ingestion` (durable, with DLQ)
- **Dead Letter Queue**: `transcripts.failed`

**Event Payload Structure**:
```json
{
  "meetingId": "01K78EDVB8F67J0SMJE31331NC",
  "eventType": "Transcription completed",
  "transcriptUrl": "https://fireflies.ai/view/...",
  "audioUrl": "https://fireflies.ai/audio/...",
  "videoUrl": "https://fireflies.ai/video/...",
  "transcript": {
    "title": "Meeting Title",
    "date": "2025-10-11T...",
    "duration": 3600,
    "participants": ["Alice", "Bob"],
    "sentences": [{"text": "...", "speaker_name": "...", "start_time": 0}],
    "summary": "Meeting summary overview"
  },
  "metadata": {
    "timestamp": "2025-10-11T02:00:00.000Z",
    "source": "n8n-fireflies-workflow",
    "version": "1.0.0",
    "workflowId": "Gjb9aPqfpAjLhe9z",
    "executionId": "..."
  }
}
```

**Fireflies API Field Mapping**:
- `transcript_url` → transcriptUrl
- `audio_url` → audioUrl
- `video_url` → videoUrl
- `meeting_attendees[].name` → participants[]
- `summary.overview` or `summary.short_summary` → summary
- `sentences[]` → transcript.sentences (array of objects)

### 2. n8n Workflow Modifications (`updated-workflow-nodes.json`)

Required changes to the existing workflow:

#### a. Update "Get a transcript" node
Ensure it fetches the complete transcript data including URL.

#### b. Add "Transform for RabbitMQ" node (NEW)
Insert between "Get a transcript" and "RabbitMQ" nodes:
- Type: Function node
- Purpose: Transform Fireflies data into standardized event payload
- Adds metadata (timestamp, workflow ID, execution ID)

#### c. Update "RabbitMQ" node
Configure for durable event publishing:
- Mode: `sendToExchange`
- Exchange: `fireflies.events`
- Routing Key: `fireflies.transcript.completed`
- Options: `durable: true`, `persistent: true`

### 3. RAG Consumer (`../scripts/rag_transcript_consumer.py`)

Python consumer that:
- Connects to RabbitMQ queue
- Processes transcript events
- Ingests into RAG system (placeholder implementation)
- Handles retries and dead-letter queue routing

**Features**:
- Manual acknowledgment
- Retry logic with exponential backoff (max 3 retries)
- Dead-letter queue for failed messages
- Configurable via environment variables

### 4. RabbitMQ Setup Script (`../scripts/setup_rabbitmq.sh`)

Automated setup script that creates:
- Main exchange (`fireflies.events`)
- Dead-letter exchange (`fireflies.events.dlx`)
- Main queue (`transcripts.rag.ingestion`)
- Dead-letter queue (`transcripts.failed`)
- Queue bindings with routing keys

## Setup Instructions

### Step 1: Setup RabbitMQ Infrastructure

```bash
# Configure connection (optional, defaults to localhost)
export RABBITMQ_HOST="localhost"
export RABBITMQ_PORT="15672"
export RABBITMQ_USER="guest"
export RABBITMQ_PASSWORD="guest"

# Run setup script
./scripts/setup_rabbitmq.sh
```

### Step 2: Update n8n Workflow

1. Open your workflow in n8n (`workflow.json`)
2. Apply changes from `updated-workflow-nodes.json`:
   - Add the "Transform for RabbitMQ" function node
   - Update the RabbitMQ node parameters
   - Update connections between nodes

### Step 3: Start RAG Consumer

```bash
# Install dependencies
pip install pika

# Configure (optional)
export RABBITMQ_HOST="localhost"
export RABBITMQ_PORT="5672"  # AMQP port, not management port
export RABBITMQ_USER="guest"
export RABBITMQ_PASSWORD="guest"

# Start consumer
python scripts/rag_transcript_consumer.py
```

### Step 4: Implement RAG Ingestion

Replace the placeholder `_ingest_to_rag()` method in the consumer with your actual RAG system integration:

```python
def _ingest_to_rag(self, document: Dict[str, Any]) -> bool:
    # Example: Vector database insertion
    # vector_db.insert(
    #     id=document['id'],
    #     text=document['content'],
    #     metadata=document['metadata']
    # )
    return True
```

## Testing

### 1. Test RabbitMQ Setup

```bash
# View queues and exchanges
open http://localhost:15672/#/queues

# Check queue is bound correctly
rabbitmqadmin list bindings
```

### 2. Test Event Publishing

Trigger the n8n workflow manually or send a test webhook from Fireflies.

### 3. Monitor Consumer

```bash
# Consumer will log all activity
python scripts/rag_transcript_consumer.py

# Expected output:
# Connected to RabbitMQ at localhost:5672
# Listening on queue: transcripts.rag.ingestion
# Waiting for transcript events...
```

## Configuration

### Environment Variables

**RabbitMQ Connection**:
- `RABBITMQ_HOST`: RabbitMQ server host (default: localhost)
- `RABBITMQ_PORT`: AMQP port for consumer (default: 5672)
- `RABBITMQ_USER`: Username (default: guest)
- `RABBITMQ_PASSWORD`: Password (default: guest)

**Queue Configuration**:
- `RABBITMQ_EXCHANGE`: Exchange name (default: fireflies.events)
- `RABBITMQ_QUEUE`: Queue name (default: transcripts.rag.ingestion)
- `RABBITMQ_ROUTING_KEY`: Routing key (default: fireflies.transcript.completed)

### Durability Settings

All components are configured for durability:

- **Exchanges**: Durable (survive broker restarts)
- **Queues**: Durable with persistent messages
- **Messages**: TTL of 24 hours, then moved to DLQ
- **DLQ Messages**: TTL of 7 days

## Monitoring & Troubleshooting

### Check Queue Depth

```bash
rabbitmqadmin list queues name messages
```

### View Failed Messages

Failed messages (after 3 retries) go to `transcripts.failed` queue:

```bash
# View DLQ in management UI
open http://localhost:15672/#/queues/%2F/transcripts.failed

# Re-publish failed messages (manual recovery)
# Use the RabbitMQ management UI "Get Messages" → "Requeue"
```

### Consumer Logs

The consumer logs all operations:
- Connection status
- Message reception
- Processing success/failure
- Retry attempts
- DLQ routing

## Architecture Decisions

### 1. Topic Exchange
- **Decision**: Use topic exchange instead of direct or fanout
- **Rationale**: Allows future consumers to subscribe to specific event types using routing key patterns (e.g., `fireflies.transcript.*`, `fireflies.*.completed`)

### 2. Manual Acknowledgment
- **Decision**: Use manual ACKs instead of auto-ACK
- **Rationale**: Ensures messages aren't lost if consumer crashes during processing

### 3. Dead Letter Queue Strategy
- **Decision**: Implement DLQ with TTL-based and retry-based routing
- **Rationale**: Prevents message loss while avoiding infinite retry loops

### 4. Separate Transform Node
- **Decision**: Add dedicated transform node instead of transforming in RabbitMQ node
- **Rationale**: Better separation of concerns, easier debugging, more maintainable

## Future Enhancements

1. **Multiple Consumers**: Scale RAG ingestion horizontally
2. **Event Types**: Add more routing keys for different event types (summary.ready, highlights.ready)
3. **Metrics**: Add Prometheus metrics for queue depth, processing time, failures
4. **Circuit Breaker**: Add circuit breaker pattern for RAG API calls
5. **Schema Validation**: Add JSON schema validation before publishing events

## References

- [RabbitMQ Topic Exchanges](https://www.rabbitmq.com/tutorials/tutorial-five-python.html)
- [n8n RabbitMQ Node](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.rabbitmq/)
- [Fireflies API Documentation](https://docs.fireflies.ai/)
