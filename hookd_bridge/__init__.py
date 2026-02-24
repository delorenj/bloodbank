"""
hookd Compatibility Bridge (GOD-4 / MIG-1)

Thin HTTP shim that accepts OpenClaw-style hook payloads and translates
them into Bloodbank CommandEnvelope messages published on the event bus.

Legacy callers (heartbeat-router, infra-dispatcher) POST to this bridge
instead of directly to OpenClaw. The bridge wraps the payload in a command
envelope and publishes to command.{agent}.{action} on Bloodbank, where the
command-adapter picks it up for FSM-guarded execution.

This is a TRANSITIONAL component. Once all callers publish commands directly
to Bloodbank, the bridge is deprecated.

See: docs/architecture/COMMAND-SYSTEM-RFC.md §6
"""
