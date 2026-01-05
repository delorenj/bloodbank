#!/usr/bin/env python3
"""
RAG Transcript Consumer
Consumes Fireflies transcript events from RabbitMQ and ingests them into RAG system
"""

import pika
import json
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone
import time

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class TranscriptRAGConsumer:
    """Consumer for processing Fireflies transcripts and ingesting into RAG"""

    def __init__(
        self,
        rabbitmq_host: str = "localhost",
        rabbitmq_port: int = 5672,
        rabbitmq_user: str = "guest",
        rabbitmq_password: str = "guest",
        exchange_name: str = "fireflies.events",
        queue_name: str = "transcripts.rag.ingestion",
        routing_key: str = "fireflies.transcript.completed",
    ):
        self.rabbitmq_host = rabbitmq_host
        self.rabbitmq_port = rabbitmq_port
        self.rabbitmq_user = rabbitmq_user
        self.rabbitmq_password = rabbitmq_password
        self.exchange_name = exchange_name
        self.queue_name = queue_name
        self.routing_key = routing_key

        self.connection: Optional[pika.BlockingConnection] = None
        self.channel: Optional[pika.channel.Channel] = None
        self.retry_count = 0
        self.max_retries = 3

    def connect(self) -> bool:
        """Establish connection to RabbitMQ"""
        try:
            credentials = pika.PlainCredentials(
                self.rabbitmq_user, self.rabbitmq_password
            )

            parameters = pika.ConnectionParameters(
                host=self.rabbitmq_host,
                port=self.rabbitmq_port,
                credentials=credentials,
                heartbeat=600,
                blocked_connection_timeout=300,
            )

            self.connection = pika.BlockingConnection(parameters)
            self.channel = self.connection.channel()

            # Declare exchange (idempotent)
            self.channel.exchange_declare(
                exchange=self.exchange_name, exchange_type="topic", durable=True
            )

            # Declare queue with dead-letter configuration
            self.channel.queue_declare(
                queue=self.queue_name,
                durable=True,
                arguments={
                    "x-message-ttl": 86400000,  # 24 hours
                    "x-dead-letter-exchange": f"{self.exchange_name}.dlx",
                    "x-dead-letter-routing-key": "transcript.failed",
                },
            )

            # Bind queue to exchange
            self.channel.queue_bind(
                queue=self.queue_name,
                exchange=self.exchange_name,
                routing_key=self.routing_key,
            )

            # Set QoS - prefetch 1 message at a time
            self.channel.basic_qos(prefetch_count=1)

            logger.info(
                f"Connected to RabbitMQ at {self.rabbitmq_host}:{self.rabbitmq_port}"
            )
            logger.info(f"Listening on queue: {self.queue_name}")
            logger.info(
                f"Exchange: {self.exchange_name}, Routing key: {self.routing_key}"
            )

            return True

        except Exception as e:
            logger.error(f"Failed to connect to RabbitMQ: {e}")
            return False

    def process_transcript(self, event_data: Dict[str, Any]) -> bool:
        """
        Process a transcript event and ingest into RAG system

        Args:
            event_data: The event payload from RabbitMQ

        Returns:
            bool: True if processing succeeded, False otherwise
        """
        try:
            meeting_id = event_data.get("meetingId")
            transcript = event_data.get("transcript", {})
            transcript_url = event_data.get("transcriptUrl")
            metadata = event_data.get("metadata", {})

            logger.info(f"Processing transcript for meeting: {meeting_id}")
            logger.info(f"Transcript URL: {transcript_url}")
            logger.info(f"Event timestamp: {metadata.get('timestamp')}")

            # Extract transcript content
            transcript_text = self._extract_transcript_text(transcript)

            if not transcript_text:
                logger.warning(f"No transcript text found for meeting {meeting_id}")
                return False

            # Prepare document for RAG ingestion
            document = {
                "id": meeting_id,
                "title": transcript.get("title", f"Transcript {meeting_id}"),
                "content": transcript_text,
                "metadata": {
                    "source": "fireflies",
                    "meeting_id": meeting_id,
                    "date": transcript.get("date"),
                    "duration": transcript.get("duration"),
                    "participants": transcript.get("participants", []),
                    "summary": transcript.get("summary"),
                    "url": transcript_url,
                    "audio_url": event_data.get("audioUrl"),
                    "video_url": event_data.get("videoUrl"),
                    "ingested_at": datetime.now(timezone.utc).isoformat(),
                    "workflow_id": metadata.get("workflowId"),
                    "execution_id": metadata.get("executionId"),
                },
            }

            # TODO: Replace with actual RAG ingestion logic
            # This is a placeholder for your RAG system integration
            success = self._ingest_to_rag(document)

            if success:
                logger.info(f"Successfully ingested transcript {meeting_id} into RAG")
                return True
            else:
                logger.error(f"Failed to ingest transcript {meeting_id} into RAG")
                return False

        except Exception as e:
            logger.error(f"Error processing transcript: {e}", exc_info=True)
            return False

    def _extract_transcript_text(self, transcript: Dict[str, Any]) -> str:
        """Extract text content from transcript object"""
        # Handle different transcript formats
        if isinstance(transcript.get("sentences"), list):
            # Format: array of sentence objects
            return " ".join(
                [
                    s.get("text", "")
                    for s in transcript["sentences"]
                    if isinstance(s, dict) and s.get("text")
                ]
            )
        elif isinstance(transcript.get("sentences"), str):
            # Format: raw text
            return transcript["sentences"]
        else:
            # Fallback to any text field
            return str(transcript.get("text", ""))

    def _ingest_to_rag(self, document: Dict[str, Any]) -> bool:
        """
        Ingest document into RAG system

        TODO: Implement actual RAG ingestion logic here
        This is a placeholder that you should replace with your RAG system's API

        Examples:
        - Vector database insertion (Pinecone, Weaviate, Qdrant)
        - Embedding generation (OpenAI, Cohere)
        - Document chunking and indexing
        """
        logger.info(f"[PLACEHOLDER] Ingesting document to RAG: {document['id']}")
        logger.info(f"Content length: {len(document['content'])} characters")
        logger.info(f"Metadata: {document['metadata']}")

        # Simulate processing time
        time.sleep(0.5)

        # Return success for now - replace with actual implementation
        return True

    def callback(
        self,
        ch: pika.channel.Channel,
        method: pika.spec.Basic.Deliver,
        properties: pika.spec.BasicProperties,
        body: bytes,
    ):
        """Callback for processing messages from RabbitMQ"""
        try:
            # Parse message
            event_data = json.loads(body.decode("utf-8"))

            logger.info(f"Received event: {event_data.get('eventType')}")

            # Process the transcript
            success = self.process_transcript(event_data)

            if success:
                # Acknowledge message
                ch.basic_ack(delivery_tag=method.delivery_tag)
                self.retry_count = 0
            else:
                # Reject and requeue if retries available
                if self.retry_count < self.max_retries:
                    self.retry_count += 1
                    logger.warning(
                        f"Requeuing message (retry {self.retry_count}/{self.max_retries})"
                    )
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                    time.sleep(5 * self.retry_count)  # Exponential backoff
                else:
                    # Send to dead letter queue
                    logger.error("Max retries exceeded, sending to DLQ")
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                    self.retry_count = 0

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse message body: {e}")
            # Reject malformed messages without requeue
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        except Exception as e:
            logger.error(f"Unexpected error in callback: {e}", exc_info=True)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

    def start_consuming(self):
        """Start consuming messages from the queue"""
        try:
            if not self.connection or not self.channel:
                if not self.connect():
                    logger.error("Failed to establish connection")
                    return

            logger.info("Starting consumer...")
            self.channel.basic_consume(
                queue=self.queue_name, on_message_callback=self.callback, auto_ack=False
            )

            logger.info("Waiting for transcript events. Press CTRL+C to exit.")
            self.channel.start_consuming()

        except KeyboardInterrupt:
            logger.info("Consumer stopped by user")
            self.stop_consuming()
        except Exception as e:
            logger.error(f"Error in consumer: {e}", exc_info=True)
            self.stop_consuming()

    def stop_consuming(self):
        """Stop consuming and close connections"""
        try:
            if self.channel:
                self.channel.stop_consuming()
            if self.connection:
                self.connection.close()
            logger.info("Consumer stopped and connections closed")
        except Exception as e:
            logger.error(f"Error stopping consumer: {e}")


def main():
    """Main entry point"""
    import os

    # Configuration from environment variables or defaults
    consumer = TranscriptRAGConsumer(
        rabbitmq_host=os.getenv("RABBITMQ_HOST", "localhost"),
        rabbitmq_port=int(os.getenv("RABBITMQ_PORT", 5672)),
        rabbitmq_user=os.getenv("RABBITMQ_USER", "guest"),
        rabbitmq_password=os.getenv("RABBITMQ_PASSWORD", "guest"),
        exchange_name=os.getenv("RABBITMQ_EXCHANGE", "fireflies.events"),
        queue_name=os.getenv("RABBITMQ_QUEUE", "transcripts.rag.ingestion"),
        routing_key=os.getenv("RABBITMQ_ROUTING_KEY", "fireflies.transcript.completed"),
    )

    consumer.start_consuming()


if __name__ == "__main__":
    main()
