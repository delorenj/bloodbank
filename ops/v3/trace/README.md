# Bloodbank v3 tracing — operator workflow

This directory documents the **operator-facing tracing workflow** for the v3
platform. It defines how correlation, causation, and W3C trace context are
expected to flow across adapters, producers, consumers, and replays. No
runtime trace tooling exists yet.

Companion docs:

- `../replay/README.md` — replay identity preservation and headers.
- `../../../compose/v3/nats/README.md` — subject topology used when walking
  a correlation chain.
- Metarepo plan: `../../../../docs/architecture/v3-implementation-plan.md`.
- ADR-0001: `../../../../docs/architecture/ADR-0001-v3-platform-pivot.md`.

## Status

**Production trace tooling is not yet implemented.** OpenTelemetry export is
intended to happen at the Dapr level via Dapr's tracing configuration, not
by hand-instrumenting producers. That wiring is deferred. This document is
the contract the tooling must satisfy.

## Core identifiers

The v3 CloudEvents envelope (see `../../../../docs/architecture/v3-implementation-plan.md#message-contracts`)
carries three identifiers the operator uses to follow a workflow:

| Field           | Source                                 | Meaning                                                                 |
|-----------------|----------------------------------------|-------------------------------------------------------------------------|
| `correlationid` | Envelope field on every v3 envelope.   | Identifies the business transaction that ties related messages together. |
| `causationid`   | Envelope field; nullable.              | The immediate parent message's `id`. Null for root messages.            |
| `traceparent`   | Envelope field; W3C Trace Context.     | Distributed trace id + span id + flags, per the W3C spec.               |

The command envelope uses `correlation_id` and `causation_id` (underscored)
for historical reasons; semantically identical to their CloudEvents cousins.

## What each identifier is for

- **`correlationid` — group by workflow.** Every event and command that
  belongs to one logical operator intent shares a single `correlationid`.
  Two independent workflows never share one. Operators look up "the whole
  workflow" by filtering on a single `correlationid`.
- **`causationid` — the immediate parent.** Exactly one message caused the
  current message. `causationid` points to it by `id`. This is **not** the
  full ancestry chain; walk it one hop at a time to reconstruct history.
- **`traceparent` — distributed trace.** W3C-standard value that any
  compliant backend (Jaeger, Tempo, OpenTelemetry Collector, etc.) can
  correlate. Distinct from `correlationid`: `traceparent` groups system-level
  spans; `correlationid` groups business-level messages.

## Propagation rules

- Every v3 event and command MUST carry `correlationid` / `correlation_id`.
- Every derived v3 event or command MUST carry `causationid` /
  `causation_id` when the upstream message is known.
- `traceparent` MUST be propagated unchanged when a runtime can forward the
  existing trace context. Adapters that cannot forward (e.g. a webhook
  entry-point with no inbound trace) MUST start a fresh trace but still
  attach the new root span to the same `correlationid`.
- Do not invent a new `correlationid` simply because an adapter is
  translating a payload. Translation preserves correlation.

## Replays and tracing

Replays preserve the original `correlationid`, `causationid`, and
`traceparent` (see `../replay/README.md`). A replay may create its own
runtime span; that span SHOULD record the `Bb-Replay-Batch-Id` header value
as an attribute so trace backends can filter replay spans from first-delivery
spans.

## OpenTelemetry integration (deferred)

Runtime OTel integration happens at the Dapr level via Dapr's tracing
configuration (`tracing:` block in a Dapr configuration resource). Services
do not instrument publish/subscribe paths by hand. When OTel wiring lands:

- All Dapr sidecars share one tracing configuration.
- `traceparent` on the CloudEvents envelope is the inbound / outbound carrier.
- A trace backend (e.g. Jaeger, Tempo) becomes part of the `compose/v3/`
  sandbox.

None of this is wired today.

## Operator workflow (forward-looking, NATS-level)

Until a trace backend exists, the operator's workflow is subject queries
against NATS. The commands below are **doc-only**; they describe the shape
of the eventual tooling. `bb_v3 trace` is a stub.

Given a `correlationid` value `abc-123`, the operator expects to:

```text
# 1. Pull all events and commands in the correlation group.
nats stream view BLOODBANK_V3_EVENTS   --subject 'event.>'    --filter 'data.correlationid == "abc-123"'
nats stream view BLOODBANK_V3_COMMANDS --subject 'command.>'  --filter 'data.correlation_id == "abc-123"'
nats stream view BLOODBANK_V3_COMMANDS --subject 'reply.>'    --filter 'data.correlation_id == "abc-123"'

# 2. Walk causation links.
# Pick any single message. Find its parent:
#   parent.id == current.causationid
# Repeat until causationid is null (root).

# 3. Pull the distributed trace.
# traceparent on the envelope => lookup in the trace backend (not yet wired).
```

`nats stream view` with `--filter` is illustrative. Any concrete query tool
(native NATS CLI, a JetStream consumer group replaying into a grep, Apicurio
SDKs, etc.) that performs the same correlation lookup satisfies the contract.

## What `bb_v3 trace` will do (forward-looking)

```text
bb_v3 trace <correlationid>
  - prints every event, command, and reply in the correlation group,
  - orders them by `time`,
  - annotates each with its `causationid` parent,
  - surfaces `traceparent` so the operator can paste it into a trace UI.
```

Not implemented yet. The `cli/v3/bb_v3.py` stub has a placeholder.
