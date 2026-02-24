"""
Agent Command Adapter (GOD-3 / AGENT-CTRL-1)

FastStream consumer that:
1. Subscribes to command.{agent}.# on Bloodbank
2. Runs FSM guards (TTL, idempotency, state)
3. Dispatches to OpenClaw via hooks API
4. Publishes ack/result/error lifecycle events

See: docs/architecture/COMMAND-SYSTEM-RFC.md §5
"""
