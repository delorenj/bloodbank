# NATS JetStream topology

This directory documents the NATS side of Bloodbank v3.

## Streams

- `BLOODBANK_V3_EVENTS` stores immutable event traffic published on `event.>`.
- `BLOODBANK_V3_COMMANDS` stores command traffic published on `command.>`.

## Subject conventions

- `event.>` is for CloudEvents-style immutable facts.
- `command.>` is for mutable requests that expect an action.
- `reply.>` is for transient command replies and should not be treated as event history.

## Replay posture

- Replay is supported for event traffic.
- Commands are operational messages and should not be used as replay input.
- Future replay tooling should read from the event stream, not from command or reply subjects.

## Dead-letter assumptions

- Dead-letter handling is intentionally not baked into the scaffold.
- Add per-consumer dead-letter policies in later operational tickets.
- Keep dead-letter handling separate from the legacy RabbitMQ model.

## Notes

- The topology uses placeholder-safe local values only.
- The topology is intentionally independent of v2 RabbitMQ services.
