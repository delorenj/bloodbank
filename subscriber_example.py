import asyncio
import orjson
import aio_pika
from pydantic import ValidationError
from config import settings
from events import EventEnvelope


async def main():
    """
    An example of a standalone subscriber service.
    """
    connection = await aio_pika.connect_robust(settings.rabbit_url)
    channel = await connection.channel()

    # Ensure the exchange exists
    exchange = await channel.declare_exchange(
        settings.exchange_name, aio_pika.ExchangeType.TOPIC, durable=True
    )

    # Declare a queue.
    # A durable queue will survive a broker restart.
    # An exclusive queue is only accessible by the current connection and is deleted when the connection closes.
    # You can choose a specific queue name or let RabbitMQ generate one.
    queue = await channel.declare_queue(name="my_subscriber_queue", durable=True)

    # Bind the queue to the exchange with a routing key.
    # The routing key determines which messages the queue will receive.
    #
    # Examples:
    # - "llm.prompt" will only receive messages with that exact key.
    # - "artifact.*" will receive messages for artifact.created, artifact.updated, etc.
    # - "#" will receive all messages published to the exchange.
    binding_key = "#"  # Listen for all events
    await queue.bind(exchange, routing_key=binding_key)

    print(
        f"[*] Waiting for messages with binding key '{binding_key}'. To exit press CTRL+C"
    )

    async with queue.iterator() as queue_iter:
        async for message in queue_iter:
            async with message.process():
                try:
                    # The body is a JSON string, so we parse it.
                    body = orjson.loads(message.body)

                    # We can validate the incoming data against our Pydantic model.
                    envelope = EventEnvelope.model_validate(body)

                    print("\n[✔] Received Event:")
                    print(f"  - ID: {envelope.id}")
                    print(f"  - Timestamp: {envelope.ts}")
                    print(f"  - Event Type: {envelope.event_type}")
                    print(f"  - Source: {envelope.source}")
                    print(f"  - Payload: {envelope.data}")

                    # Here you would add your own logic to process the event,
                    # for example, saving to a database, calling another API, etc.

                except (ValidationError, Exception) as e:
                    print(f"\n[!] Failed to process message: {e}")
                    print(f"  - Raw body: {message.body.decode('utf-8')}")

                if queue.name in message.body.decode():
                    break


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Subscriber stopped.")
