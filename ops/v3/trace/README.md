# Bloodbank v3 trace guide

This directory defines the trace and correlation rules that Bloodbank v3 must
follow across adapters, commands, and events.

## Core identifiers

- `correlation_id` identifies the business transaction that ties related
  messages together.
- `causation_id` identifies the immediate upstream message that caused the
  current message to exist.
- `traceparent` carries the W3C Trace Context value used by runtime tracing
  systems.

## Expectations

- Every emitted v3 event and command must carry `correlation_id`.
- Every derived v3 event or command must carry `causation_id` when the upstream
  message is known.
- `traceparent` should be propagated unchanged when the runtime can forward the
  existing trace context.
- If a message starts a new trace, the new root trace must still be linked to
  the same `correlation_id`.

## Trace propagation rules

- Preserve the original business correlation chain even when message transport
  changes.
- Do not invent a new `correlation_id` simply because an adapter is translating
  a payload.
- Use `causation_id` to show the immediate parent message, not the entire
  ancestry chain.
- Keep trace metadata alongside contract metadata so operators can search both
  at the same time.

## Replay and trace

- Replayed messages must keep the original `correlation_id`.
- Replayed messages must keep the original `causation_id` in replay metadata.
- Replayed messages should surface the original `traceparent` for audit and
  diagnosis.
- Replay execution may create a fresh runtime span, but it must not erase the
  original business trace chain.

## Status

This document defines the target expectations only.
It does not claim a production trace pipeline has already been implemented.
