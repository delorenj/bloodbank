#!/usr/bin/env python3
"""
Bloodbank WebSocket Relay - Minimal event broadcaster
Connects to RabbitMQ and broadcasts events to WebSocket clients.
"""
import asyncio
import logging
import os
import orjson
from typing import Set
import aio_pika
import websockets
from websockets.server import WebSocketServerProtocol

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration from environment
RABBIT_URL = os.getenv("RABBIT_URL", "amqp://delorenj:MISSING_PASSWORD@rabbitmq:5672/")
EXCHANGE_NAME = os.getenv("EXCHANGE_NAME", "bloodbank.events.v1")
ROUTING_KEY = os.getenv("ROUTING_KEY", "#")
WS_HOST = os.getenv("WS_HOST", "0.0.0.0")
WS_PORT = int(os.getenv("WS_PORT", "8683"))

# Active WebSocket clients
clients: Set[WebSocketServerProtocol] = set()


async def broadcast_event(event_data: dict):
    """Broadcast event to all connected WebSocket clients."""
    if not clients:
        return
    
    message = orjson.dumps(event_data)
    dead_clients = []
    
    for client in clients:
        try:
            await client.send(message)
        except Exception as e:
            logger.warning(f"Failed to send to client: {e}")
            dead_clients.append(client)
    
    # Clean up dead connections
    for client in dead_clients:
        clients.discard(client)
        logger.info(f"Removed dead client. Active: {len(clients)}")


async def rabbitmq_consumer():
    """Consume events from RabbitMQ and broadcast to WebSocket clients."""
    logger.info(f"Connecting to RabbitMQ: {RABBIT_URL}")
    
    connection = await aio_pika.connect_robust(RABBIT_URL)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=10)
    
    exchange = await channel.declare_exchange(
        EXCHANGE_NAME, 
        aio_pika.ExchangeType.TOPIC, 
        durable=True
    )
    
    # Create exclusive queue for this relay instance
    queue = await channel.declare_queue("", exclusive=True)
    
    await queue.bind(exchange, routing_key=ROUTING_KEY)
    logger.info(f"Bound to exchange '{EXCHANGE_NAME}' with routing key '{ROUTING_KEY}'")
    
    async def on_message(message: aio_pika.IncomingMessage):
        async with message.process():
            try:
                payload = orjson.loads(message.body)
                
                # Add routing key and type tag for client context
                event_data = {
                    "type": "event",
                    "routing_key": message.routing_key,
                    "envelope": payload
                }
                
                await broadcast_event(event_data)
                logger.debug(f"Broadcasted event: {message.routing_key}")
                
            except Exception as e:
                logger.error(f"Error processing message: {e}", exc_info=True)
                raise
    
    await queue.consume(on_message)
    logger.info("RabbitMQ consumer started")
    
    # Keep running
    try:
        await asyncio.Future()  # Run forever
    except asyncio.CancelledError:
        logger.info("Consumer cancelled")
        await connection.close()


async def websocket_handler(websocket: WebSocketServerProtocol):
    """Handle WebSocket client connections."""
    clients.add(websocket)
    logger.info(f"Client connected. Active: {len(clients)}")
    
    try:
        # Send welcome message
        await websocket.send(orjson.dumps({
            "type": "welcome",
            "message": "Connected to Bloodbank WebSocket Relay",
            "exchange": EXCHANGE_NAME,
            "routing_key": ROUTING_KEY
        }))
        
        # Keep connection alive and handle pings
        async for message in websocket:
            # Echo back any messages (for ping/pong)
            if message == "ping":
                await websocket.send(orjson.dumps({"type": "pong"}))
                
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        clients.discard(websocket)
        logger.info(f"Client disconnected. Active: {len(clients)}")


async def main():
    """Start WebSocket server and RabbitMQ consumer."""
    logger.info("Starting Bloodbank WebSocket Relay")
    logger.info(f"WebSocket: ws://{WS_HOST}:{WS_PORT}")
    logger.info(f"RabbitMQ: {EXCHANGE_NAME} / {ROUTING_KEY}")
    
    # Start RabbitMQ consumer
    consumer_task = asyncio.create_task(rabbitmq_consumer())
    
    # Start WebSocket server
    async with websockets.serve(websocket_handler, WS_HOST, WS_PORT):
        logger.info(f"WebSocket server listening on {WS_HOST}:{WS_PORT}")
        await consumer_task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
