# infra_dispatcher v3 adapter scaffold

This directory documents the v3 replacement for the current v2
`event_producers/infra_dispatcher.py` dispatcher path.

## Purpose

- Replace the legacy Plane webhook dispatcher with a thin adapter that emits
  Holyfields-generated command contracts.
- Send dispatcher output through Dapr and NATS rather than constructing a
  custom publish path.
- Keep ticket selection logic separate from contract and transport ownership.

## Target v3 publish path

Plane webhook payload -> adapter translation -> Holyfields-generated command
envelope and SDK -> Dapr pub/sub sidecar -> NATS command subject

## Contract expectations

- The adapter must preserve the incoming `correlation_id`, `causation_id`, and
  `traceparent` whenever those fields are present.
- The adapter must emit approved Holyfields command types instead of a flat
  legacy payload shape.
- The adapter must not introduce a new local envelope contract.

## Status

This is a documentation scaffold only.
No executable migration code is present here yet.
