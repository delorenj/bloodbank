# openclaw v3 adapter scaffold

Documentation-only scaffold for the v3 replacement of the legacy **openclaw**
agent hooks. No executable migration code lives here yet.

## v2 component being replaced

`openclaw_bridge` (this repository, pre-v3). Today, openclaw agent hooks emit
via the **legacy publisher**: they build a local envelope, hand it off, and
the event lands on RabbitMQ.

This path couples openclaw to:

- A hand-rolled envelope with no registered schema.
- RabbitMQ routing keys that serve as de-facto event types.
- The v2 Bloodbank publisher role, which the v3 architecture removes.

## v3 target publish path

```text
openclaw agent hook payload
  -> adapter translation (this directory, future)
  -> Holyfields-generated Python SDK
       (CloudEvents envelope construction for events,
        command envelope construction for commands)
  -> Dapr pub/sub component  `bloodbank-v3-pubsub`
  -> NATS JetStream subject  `event.<domain>.<entity>.<action>`
                          or `command.<target>.<verb>`
```

Same pattern as every v3 adapter: translation only; envelope from Holyfields;
transport via Dapr.

## Contract expectations

- Outbound messages MUST use Holyfields-generated schemas. The adapter MUST
  NOT define local envelope definitions.
- `correlationid` / `correlation_id`, `causationid` / `causation_id`, and
  `traceparent` MUST survive translation intact whenever the incoming
  payload already carries them.
- The adapter MUST NOT own catalog generation, schema registry sync, or SDK
  generation — all of those live in Holyfields.

## Unknowns to resolve

1. **Schema mapping.** Which openclaw agent-hook events map to which
   Holyfields-registered schemas? The mapping is pending Holyfields output
   (see `../../../docs/architecture/v3-holyfields-contract-work.md`,
   HOLYF-2). Same pattern as hookd.
2. **Agent-session identity.** Openclaw emits messages tied to a specific
   agent session. Whether session identity is represented as
   `source` / `subject` on the CloudEvents envelope, or via a domain-specific
   extension field, is a decision for the openclaw owner and the Holyfields
   schema author.
3. **Cutover coordination with hookd.** Openclaw and hookd share part of the
   inbound path in production. Their v3 cutovers should land close together
   to avoid running two parallel envelope conventions for longer than
   necessary. Coordination point: metarepo v3 plan.

## Blocks

This adapter is **blocked on HOLYF-2** (`../../../docs/architecture/v3-holyfields-contract-work.md`).
Holyfields must deliver the base envelope schemas and the Python SDK before
real adapter code can be written.

## Status

Documentation scaffold only. No executable migration code is present here
yet.
