# n8n Bloodbank nodes

The package contains three schema-backed nodes:

- Bloodbank publishes a selected canonical event.
- Bloodbank Trigger starts workflows from events or commands.
- Plane to Bloodbank verifies, normalizes, and publishes Plane webhooks.

## Bloodbank Trigger

Choose Events to bind one or more event schemas. Event delivery is always
asynchronous. The list also includes Plane provenance aliases such as On Ticket
Created (plane.ticket.created); aliases subscribe to the canonical repo subject
and filter data.provider_event_type.

Choose Command to bind exactly one command schema. A queue group preserves
single-consumer dispatch among equivalent n8n workflows.

- Asynchronous command processing starts the workflow and publishes no reply.
- Synchronous command processing waits for the n8n run to finish and publishes
  a correlated Bloodbank reply on the matching bloodbank.rpy subject.

The trigger uses the maintained official NATS Node transport and reconnects
automatically. Defaults use the localhost service hostname and can be overridden
per node.

## Plane ingress

Import the versioned workflow:

    n8n import:workflow --input=../n8n-workflows/plane-bloodbank.v1.json
    n8n update:workflow --id=iMw484J1ZCqKME2C --active=true

The Webhook node must retain Raw Body. Plane to Bloodbank rejects unsigned or
invalid requests before publishing. Its signing secret parameter accepts only
op:// or env:// references; raw values are rejected. The committed workflow
uses op://DeLoSecrets/PlaneWebhook/credential.

Routing metadata comes from ~/.hermes/agents-registry.yaml. Plane project IDs
map to repo slug, workspace, and project identifier without embedding host
addresses or credentials.

## Development and verification

    npm ci
    npm test
    npm run test:live
    npm run deploy

npm test covers schema generation, trigger configuration, canonical envelopes,
Plane creation/transition/comment normalization, and provenance alias filters.
npm run test:live proves multi-event subscriptions, command queue competition,
and synchronous command replies against the live NATS service.
