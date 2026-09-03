<div align="center">

<img src="assets/bloodbank.png" alt="Bloodbank" width="176">

# n8n Bloodbank nodes

Schema-backed publisher, consumer trigger, and Plane webhook ingress for the
33GOD Bloodbank NATS bus.

</div>

## Nodes

| Node | Group | What it does |
| --- | --- | --- |
| **Bloodbank** | output | Publishes a canonical event or registry-routed invocation command. Also usable as an agent tool. |
| **Bloodbank Trigger** | trigger | Starts workflows from events or single-consumer commands. |
| **Plane → Bloodbank** | transform | Verifies, normalizes, and publishes Plane webhooks. |

Each node carries the Bloodbank mark as a light/dark icon pair, so the canvas
reads correctly in either n8n theme. Plane → Bloodbank adds an inbound arrow to
mark it as the edge where outside traffic enters the bus.

## Bloodbank publisher

Event mode remains the default for existing workflows. Command mode publishes
`bloodbank.agent.invocation.start` to
`bloodbank.cmd.agent.invocation.start`. It requires a repository, non-empty
prompt, and retry-stable command UUID, then resolves exactly one eligible agent
from `~/.hermes/agents-registry.yaml`. Profile names remain inside the registry
and are never embedded in workflow data or the command envelope.

The finished command envelope is validated against the canonical JSON Schema
before a NATS connection is opened. Its generated idempotency key is scoped to
the resolved target and command UUID; malformed schemas, registry routes, or
command inputs therefore make zero transport attempts.

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
invalid requests before publishing. `Webhook Secret References` is a JSON
allowlist mapping each trusted Plane `webhook_id` to an `op://` or `env://`
reference; raw values are rejected. Selecting the secret by webhook ID lets one
HTTPS ingress serve multiple Plane workspaces without treating a workspace name
as a trust boundary. Unknown webhook IDs fail before Bloodbank publication.

The committed workflow trusts the production 33GOD and AutomaticAI workspace
webhook IDs. Their independent signing keys live in the DeLoSecrets items
`PlaneWebhook-33GOD` and `PlaneWebhook-AutomaticAI`.

Routing metadata comes from ~/.hermes/agents-registry.yaml. Plane project IDs
map to repo slug, workspace, and project identifier without embedding host
addresses or credentials.

The registry entry must match the repo-root `.project.json` ticket-provider
binding. Re-run the PM fleet-registry reconciliation after a board migration;
an unknown board is acknowledged as unrouted and is never guessed from the
workspace alone.

## Branding

`src/icons/` holds the icon masters, drawn from the Bloodbank mark at the
repository root. They are the single source of truth. n8n resolves a `file:`
icon next to the `.node.js` that declares it, so `npm run build` fans the
masters out into every compiled node directory and fails the build when a node
declares an icon that no master satisfies. Edit the master, never the copy
under `dist/`.

| Asset | Shipped as | Use |
| --- | --- | --- |
| `src/icons/bloodbank.svg` · `bloodbank.dark.svg` | `dist/nodes/{Bloodbank,BloodbankTrigger}/` | Publisher and trigger canvas icon |
| `src/icons/planeBloodbank.svg` · `planeBloodbank.dark.svg` | `dist/nodes/PlaneBloodbank/` | Plane ingress canvas icon |
| `assets/bloodbank.png` | package tarball | README and package listing |

Palette sampled from the source mark: `#C4222C` blood red and `#8E141C` deep
red on light canvases, lifted to `#D2242F`/`#9C1820` on dark ones; `#FAF8F7`
off-white for the orbit, pulse, and nodes; `#23252C` ink where those marks fall
outside the drop on a light canvas.

## Development and verification

    npm ci
    npm test
    npm run test:live
    npm run deploy

npm test covers schema generation, trigger and publisher configuration,
canonical envelopes, fail-closed invocation routing, Plane
creation/transition/comment normalization, provenance alias filters, and the
node icon contract. npm run test:live proves multi-event subscriptions, command
queue competition, and synchronous command replies against the live NATS
service.
