# openclaw v3 adapter scaffold

This directory documents the v3 replacement for the current v2 `openclaw_bridge`
component.

## Purpose

- Replace the legacy OpenClaw bridge layer with a thin adapter that uses
  Holyfields-generated contracts.
- Route outbound messages through Dapr and NATS instead of local transport or
  ad hoc publish logic.
- Keep OpenClaw-specific payload translation separate from contract ownership.

## Target v3 publish path

OpenClaw payload -> Holyfields-generated event or command contract -> Dapr
sidecar -> NATS event or command subject

## Contract expectations

- Outbound messages must use Holyfields-generated schemas, not local envelope
  definitions.
- `correlation_id`, `causation_id`, and `traceparent` must survive translation
  intact whenever the incoming payload already carries them.
- The adapter must not own catalog generation, schema registry sync, or SDK
  generation.

## Status

This is a documentation scaffold only.
No executable migration code is present here yet.
