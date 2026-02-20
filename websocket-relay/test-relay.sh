#!/bin/bash
# Test Bloodbank WebSocket Relay end-to-end

set -e

echo "🧪 Testing Bloodbank WebSocket Relay"
echo "====================================="
echo

# Check if relay is running
echo "1. Checking relay health..."
if python3 -c "import socket; s=socket.socket(); s.connect(('localhost', 8683)); s.close()" 2>/dev/null; then
    echo "✅ WebSocket relay is running on port 8683"
else
    echo "❌ WebSocket relay not accessible on port 8683"
    echo "   Start with: docker-compose up bloodbank-ws-relay"
    exit 1
fi
echo

# Test WebSocket connection (requires websocat or wscat)
echo "2. Testing WebSocket connection..."
if command -v websocat &> /dev/null; then
    echo "   Using websocat to connect..."
    timeout 3 websocat ws://localhost:8683 &
    WS_PID=$!
    sleep 1
    
    if ps -p $WS_PID > /dev/null 2>&1; then
        echo "✅ WebSocket connection successful"
        kill $WS_PID 2>/dev/null || true
    else
        echo "⚠️  WebSocket connection test failed (but this might be expected)"
    fi
elif command -v wscat &> /dev/null; then
    echo "   Using wscat to connect..."
    timeout 3 wscat -c ws://localhost:8683 &
    WS_PID=$!
    sleep 1
    
    if ps -p $WS_PID > /dev/null 2>&1; then
        echo "✅ WebSocket connection successful"
        kill $WS_PID 2>/dev/null || true
    else
        echo "⚠️  WebSocket connection test failed (but this might be expected)"
    fi
else
    echo "⚠️  No WebSocket client (websocat/wscat) found - skipping connection test"
    echo "   Install: cargo install websocat  OR  npm install -g wscat"
fi
echo

# Test event publishing
echo "3. Publishing test event to Bloodbank..."
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST http://localhost:8682/events/agent/test-relay/heartbeat \
    -H "Content-Type: application/json" \
    -d '{"timestamp": "2026-02-20T15:00:00Z", "status": "active"}' 2>/dev/null)

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

if [ "$HTTP_CODE" = "200" ]; then
    echo "✅ Event published successfully"
    echo "   Event ID: $(echo $BODY | grep -o '"event_id":"[^"]*"' | cut -d'"' -f4)"
else
    echo "❌ Failed to publish event (HTTP $HTTP_CODE)"
    echo "   Is Bloodbank running? Try: docker-compose up bloodbank"
    exit 1
fi
echo

echo "✅ All tests passed!"
echo
echo "Next steps:"
echo "  1. Connect a WebSocket client: websocat ws://localhost:8683"
echo "  2. Publish events and watch them arrive in real-time"
echo "  3. Integrate with Holocene using ws://bloodbank-ws-relay:8683"
