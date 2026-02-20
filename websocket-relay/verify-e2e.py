#!/usr/bin/env python3
"""
End-to-end verification script for Bloodbank WebSocket Relay.
Connects to relay, publishes an event to Bloodbank, verifies relay receives it.
"""
import asyncio
import json
import websockets
import sys
from datetime import datetime

RELAY_WS_URL = "ws://localhost:8683"
BLOODBANK_API_URL = "http://localhost:8682"

async def verify_relay():
    print("🧪 Bloodbank WebSocket Relay E2E Verification")
    print("=" * 50)
    
    # Connect to relay
    print(f"\n1. Connecting to relay at {RELAY_WS_URL}...")
    try:
        async with websockets.connect(RELAY_WS_URL) as ws:
            print("✅ Connected to WebSocket relay")
            
            # Receive welcome message
            welcome = await asyncio.wait_for(ws.recv(), timeout=5)
            welcome_data = json.loads(welcome)
            print(f"   Welcome: {welcome_data.get('message', 'N/A')}")
            print(f"   Exchange: {welcome_data.get('exchange', 'N/A')}")
            print(f"   Routing key: {welcome_data.get('routing_key', 'N/A')}")
            
            # Publish test event to Bloodbank
            print("\n2. Publishing test event to Bloodbank...")
            import urllib.request
            
            event_data = {
                "agent_name": "grolf",
                "status": "ok",
                "active_sessions": 1,
                "uptime_ms": 123456
            }
            
            req = urllib.request.Request(
                f"{BLOODBANK_API_URL}/events/agent/grolf/heartbeat",
                data=json.dumps(event_data).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    publish_result = json.loads(response.read())
                    event_id = publish_result.get('event_id', 'unknown')
                    print(f"✅ Event published successfully")
                    print(f"   Event ID: {event_id}")
                    print(f"   Routing key: {publish_result.get('routing_key', 'N/A')}")
            except Exception as e:
                print(f"❌ Failed to publish event: {e}")
                return False
            
            # Wait for event on WebSocket
            print("\n3. Waiting for event on WebSocket relay...")
            try:
                event_received = False
                for _ in range(5):  # Try up to 5 messages
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    msg_data = json.loads(msg)
                    
                    if msg_data.get('routing_key', '').startswith('agent.grolf'):
                        print(f"✅ Event received on WebSocket relay!")
                        print(f"   Routing key: {msg_data.get('routing_key')}")
                        print(f"   Event type: {msg_data.get('envelope', {}).get('event_type', 'N/A')}")
                        event_received = True
                        break
                
                if not event_received:
                    print("❌ Expected event not received")
                    return False
                    
            except asyncio.TimeoutError:
                print("❌ Timeout waiting for event on WebSocket")
                return False
            
            print("\n" + "=" * 50)
            print("✅ END-TO-END VERIFICATION SUCCESSFUL!")
            print("=" * 50)
            return True
            
    except websockets.exceptions.WebSocketException as e:
        print(f"❌ WebSocket error: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = asyncio.run(verify_relay())
    sys.exit(0 if success else 1)
