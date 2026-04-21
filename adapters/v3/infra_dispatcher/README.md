# infra_dispatcher v3 adapter scaffold

Documentation-only scaffold for the v3 replacement of the legacy
**infra_dispatcher**. No executable migration code lives here yet.

## v2 component being replaced

`event_producers/infra_dispatcher.py` (this repository, pre-v3). Today the
dispatcher is a **Plane-webhook -> RabbitMQ** component, internal to
Bloodbank:

- It receives Plane `webhook.plane.#` events from the v2 publisher.
- It filters for Issue events in `unstarted` state with a ready label.
- It runs the M2 component-check gate.
- It forwards qualifying tickets to OpenClaw `/hooks/agent`.

The dispatcher currently lives **inside bloodbank/** and publishes via
Bloodbank's v2 publisher. That arrangement was acceptable when Bloodbank was
the central publish path; it is not acceptable in v3, where Bloodbank owns
runtime and ops, not domain publishing.

## v3 target publish path

```text
Plane webhook payload
  -> adapter translation (this directory, future)
  -> Holyfields-generated Python SDK
       (CloudEvents envelope construction for webhook-sourced events,
        command envelope construction for dispatcher commands, e.g.
        `command.openclaw.dispatch-ticket`)
  -> Dapr pub/sub component  `bloodbank-v3-pubsub`
  -> NATS JetStream subject
```

Same pattern as the other adapters.

## Contract expectations

- The adapter MUST preserve inbound `correlation_id`, `causation_id`, and
  `traceparent` when present.
- The adapter MUST emit Holyfields-registered contract types instead of a
  flat legacy payload. No local envelope definition.
- The adapter MUST NOT introduce a parallel publish transport; Dapr
  pub/sub `bloodbank-v3-pubsub` is the only sanctioned route.

## Unknowns to resolve

1. **Repository home.** Does infra_dispatcher stay inside `bloodbank/`
   under `adapters/v3/infra_dispatcher/`, or does it split into its own
   service? Arguments either way:
   - **Stay:** the dispatcher is thin; it does nothing broker-y once v3
     lands; keeping it colocated simplifies ops.
   - **Split:** it is a domain component (Plane ingestion) with no native
     home in a broker repo; splitting it makes ownership explicit and lets
     Bloodbank shrink to runtime-only.
   **Flag for later discussion. Not decided here.**
2. **Schema mapping.** Which Plane webhook events map to which
   Holyfields-registered schemas? Pending HOLYF-2 output.
3. **Command vs. event modeling.** The ticket-dispatch flow is arguably a
   command (`command.openclaw.dispatch-ticket`) with a correlated reply,
   rather than an event. Final call lives with the infra_dispatcher owner
   and the Holyfields schema author.

## Blocks

This adapter is **blocked on HOLYF-2** (`../../../docs/architecture/v3-holyfields-contract-work.md`).
Holyfields must deliver the base envelope schemas and the Python SDK before
real adapter code can be written.

## Status

Documentation scaffold only. No executable migration code is present here
yet.
