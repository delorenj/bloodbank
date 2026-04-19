# Bloodbank v3 replay guide

This directory documents replay behavior for Bloodbank v3 before any production
replay tooling exists.

## Scope

- Replay is documented for local and controlled non-production workflows only.
- Production replay is not implemented in Bloodbank v3.
- Any future replay implementation must be explicitly approved before it can
  touch live traffic.

## Replay safety rules

- Safe replay data is immutable event data, approved command fixtures, and
  redacted operational metadata that can be re-emitted without creating new
  business intent.
- Unsafe replay data includes secrets, credentials, tokens, webhook signatures,
  and any payload that would trigger side effects beyond the original capture.
- Replay must preserve the original message identity and annotate the replay as
  a new execution record.

## Identity preservation

Replay records must preserve the original identifiers from the source message:

- `original_id` stays attached to the replay record.
- `original_correlation_id` stays attached to the replay record.
- `original_causation_id` stays attached to the replay record when one exists.
- The replay itself gets a new `replay_id` so operators can distinguish the
  replay operation from the source message.

The replayed message must keep the original business correlation chain intact.
The replay operation may add a new execution span, but it must not rewrite the
original business identifiers.

## Replay metadata

Every replay operation should record:

- `replay_id`
- `replay_requested_at`
- `replay_requested_by`
- `replay_reason`
- `replay_source`
- `replay_source_message_id`
- `replay_source_subject`
- `replay_source_stream`
- `original_id`
- `original_correlation_id`
- `original_causation_id`
- `original_traceparent`
- `replayed_at`

If a future implementation adds more fields, it must keep these names stable so
operators can search replay history consistently.

## Trace expectations during replay

- The replay record should preserve the original `traceparent` value for audit
  visibility.
- A replay execution may create its own child trace span, but the original
  `traceparent` must remain discoverable in the replay metadata.
- The correlation chain must still be readable from `correlation_id` after the
  replay is emitted.

## Status

This document is a planning scaffold only.
Production replay is not implemented yet.
