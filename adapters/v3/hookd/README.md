# hookd v3 adapter scaffold

Documentation-only scaffold for the v3 replacement of the legacy **hookd**
bridge. No executable migration code lives here yet.

## v2 component being replaced

`hookd_bridge` (this repository, pre-v3). Today, hookd publishes via the
**legacy Bloodbank CLI / RabbitMQ** path: it builds a local envelope shape,
hands it to the v2 publisher, and the event lands on the RabbitMQ
`bloodbank.events.v1` topic exchange.

This path couples hookd to:

- A hand-rolled envelope with no registered schema.
- RabbitMQ transport semantics.
- Bloodbank's in-process publisher (a role the v3 architecture explicitly
  removes from Bloodbank).

## v3 target publish path

```text
hookd inbound payload
  -> adapter translation (this directory, future)
  -> Holyfields-generated Python SDK
       (CloudEvents envelope construction for events,
        command envelope construction for commands)
  -> Dapr pub/sub component  `bloodbank-v3-pubsub`
  -> NATS JetStream subject  `event.<domain>.<entity>.<action>`
                          or `command.<target>.<verb>`
```

The adapter is **translation only**. Envelope construction is delegated to
the Holyfields SDK; transport is delegated to Dapr.

## Contract expectations

- Every outbound message MUST be built from a Holyfields-generated type.
  The adapter MUST NOT define a local replacement for the Holyfields
  envelope schemas.
- `correlationid` / `correlation_id`, `causationid` / `causation_id`, and
  `traceparent` MUST pass through unchanged when the inbound payload
  already carries them.
- Outbound traffic MUST route through Dapr `bloodbank-v3-pubsub`.

## Unknowns to resolve

The following are explicitly unresolved and MUST be settled before
executable migration code lands:

1. **Schema mapping.** Which hookd event types map to which
   Holyfields-registered schemas? The mapping table does not exist yet
   because Holyfields has not published the base CloudEvents schema or any
   service-level AsyncAPI document (see
   `../../../docs/architecture/v3-holyfields-contract-work.md`, HOLYF-2).
2. **Event vs. command classification.** Some hookd messages are better
   modeled as commands (`command.<target>.<verb>`) rather than events.
   That call lives with the hookd owner and the Holyfields schema author;
   it is not made here.
3. **Legacy-CLI-to-Dapr cutover timeline.** Hookd presently depends on the
   v2 Bloodbank CLI. A migration window with parallel-run is likely needed
   before removing the legacy path. Ownership: hookd team; coordinated
   through the metarepo v3 plan.

## Blocks

This adapter is **blocked on HOLYF-2** (`../../../docs/architecture/v3-holyfields-contract-work.md`).
Holyfields must deliver the base envelope schemas and the Python SDK before
real adapter code can be written.

## Status

Documentation scaffold only. No executable migration code is present here
yet.
