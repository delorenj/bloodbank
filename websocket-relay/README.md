# Bloodbank WebSocket Relay

Minimal standalone service that relays RabbitMQ events from Bloodbank to WebSocket clients.

## Purpose

Unblocks Holocene and other clients from receiving real-time Bloodbank events without modifying the main Bloodbank HTTP service.

## How It Works

1. Connects to RabbitMQ exchange `bloodbank.events.v1`
2. Consumes events matching routing key pattern `agent.#` (configurable)
3. Broadcasts events to all connected WebSocket clients
4. Each client receives all matching events in real-time

## Quick Start

### Using Docker Compose

```bash
# From 33GOD root
docker-compose up bloodbank-ws-relay
```

### Standalone

```bash
cd websocket-relay

# Install dependencies
pip install -r requirements.txt

# Configure (optional)
export RABBIT_URL="amqp://guest:guest@localhost:5672/"
export EXCHANGE_NAME="bloodbank.events.v1"
export ROUTING_KEY="agent.#"
export WS_PORT="8683"

# Run
python relay.py
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RABBIT_URL` | `amqp://guest:guest@localhost:5672/` | RabbitMQ connection URL |
| `EXCHANGE_NAME` | `bloodbank.events.v1` | Exchange to consume from |
| `ROUTING_KEY` | `agent.#` | Routing key pattern to bind |
| `WS_HOST` | `0.0.0.0` | WebSocket server bind address |
| `WS_PORT` | `8683` | WebSocket server port |

## WebSocket Client Example

```javascript
const ws = new WebSocket('ws://localhost:8683');

ws.onopen = () => {
    console.log('Connected to Bloodbank relay');
};

ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    
    if (data.type === 'welcome') {
        console.log('Welcome:', data.message);
    } else if (data.routing_key) {
        console.log('Event:', data.routing_key, data.envelope);
    }
};

// Keep-alive ping (optional)
setInterval(() => ws.send('ping'), 30000);
```

## Verification

### 1. Check relay is running

```bash
curl -f http://localhost:8683 || echo "WebSocket endpoint active"
```

### 2. Connect with websocat

```bash
# Install websocat: https://github.com/vi/websocat
websocat ws://localhost:8683
```

### 3. Publish test event to Bloodbank

```bash
curl -X POST http://localhost:8682/events/agent/test-agent/heartbeat \
  -H "Content-Type: application/json" \
  -d '{"timestamp": "2026-02-20T15:00:00Z", "status": "active"}'
```

You should see the event arrive in your WebSocket client.

## Architecture

```
┌─────────────┐
│  RabbitMQ   │
│  Exchange   │
│bloodbank.   │
│ events.v1   │
└──────┬──────┘
       │ agent.#
       ↓
┌─────────────────┐      WebSocket
│  WS Relay       │◄─────────────── Holocene
│  (this service) │◄─────────────── Client 2
│                 │◄─────────────── Client N
└─────────────────┘
```

## Notes

- Uses exclusive queue (auto-deleted on disconnect)
- No message persistence or replay
- Clients only receive events after connection
- For historical events, query Candystore
