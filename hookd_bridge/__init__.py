"""
hookd Compatibility Bridge (GOD-4 / MIG-1)

Thin HTTP shim that accepts OpenClaw-style hook payloads and translates
them into Bloodbank CommandEnvelope messages published on the event bus.

Callers (heartbeat-router, infra-dispatcher) POST to this bridge instead of
directly to OpenClaw. The bridge wraps the payload in a command envelope
and publishes to command.{agent}.{action} on Bloodbank, where the
command-adapter picks it up for FSM-guarded execution.

Status: KEPT. This bridge is a permanent part of the architecture. It
gives HTTP-only clients a stable command-emission surface without forcing
them to speak AMQP / Dapr directly. The earlier "transitional / will be
deprecated" framing has been reversed (2026-05-05).

NOTE ON NAMING: This package is UNRELATED to the `hookd/` Rust daemon
despite the name-prefix overlap. `hookd/` produces `tool.mutation.*` events
from Claude Code hook captures; this bridge produces `command.*` envelopes
from OpenClaw HTTP calls. The two components share no code, no events, and
no dependency. Boundary recorded in
docs/architecture/ADR-0003-hookd-boundary.md.

See: docs/architecture/COMMAND-SYSTEM-RFC.md §6
"""
