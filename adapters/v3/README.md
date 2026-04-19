# Bloodbank v3 adapter migration scaffolds

This directory holds documentation-only scaffolds for the first adapter
migration wave.

## Shared migration rules

- Each adapter must translate external payloads into Holyfields-generated
  contracts.
- Each adapter must publish through Dapr and NATS rather than inventing a new
  local transport path.
- Each adapter must preserve `correlation_id`, `causation_id`, and
  `traceparent` when it forwards a message.
- No executable migration code lives in these scaffolds yet.

## Adapter map

- `hookd/` replaces the current v2 `hookd_bridge` component.
- `openclaw/` replaces the current v2 `openclaw_bridge` component.
- `infra_dispatcher/` replaces the current v2 `event_producers/infra_dispatcher.py`
  dispatcher path.

## Target publish path

The intended v3 path for all adapters is:

Holyfields-generated contracts and SDKs -> Dapr sidecar -> NATS subject-based
publish path

The adapter directories below describe the v2 source they replace and the v3
publish path they will eventually use.
