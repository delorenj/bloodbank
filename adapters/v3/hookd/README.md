# hookd v3 adapter scaffold

This directory documents the v3 replacement for the current v2 `hookd_bridge`
component.

## Purpose

- Replace the legacy hook bridge with a thin adapter that uses
  Holyfields-generated command contracts.
- Publish through the v3 Dapr and NATS path instead of hand-building envelopes.
- Keep the adapter responsible for translation only, not schema invention.

## Target v3 publish path

hookd external payload -> Holyfields-generated command envelope and SDK -> Dapr
pub/sub sidecar -> NATS command subject

## Contract expectations

- Commands must use the Holyfields command envelope.
- `correlation_id`, `causation_id`, and `traceparent` must pass through the
  adapter unchanged whenever they are already present.
- The adapter must not define a local replacement for the Holyfields command
  schema.

## Status

This is a documentation scaffold only.
No executable migration code is present here yet.
