<div align="center">

<img src="assets/bloodbank.png" alt="Bloodbank" width="176">

# n8n Bloodbank nodes

Schema-backed publisher, consumer trigger, Plane webhook ingress, and
agent-fleet dispatch for the 33GOD Bloodbank NATS bus.

</div>

## Nodes

| Node | Group | What it does |
| --- | --- | --- |
| **Bloodbank** | output | Publishes a canonical event or registry-routed invocation command. Also usable as an agent tool. |
| **Bloodbank Trigger** | trigger | Starts workflows from events or single-consumer commands. |
| **Plane → Bloodbank** | transform | Verifies, normalizes, and publishes Plane webhooks. |
| **33GOD Agent Fleet** | output | Hands a ticket to the fleet agent that owns its board. Also usable as an agent tool. |

Each node carries the Bloodbank mark as a light/dark icon pair, so the canvas
reads correctly in either n8n theme. Plane → Bloodbank adds an inbound arrow to
mark it as the edge where outside traffic enters the bus; 33GOD Agent Fleet adds
an outbound fan to mark it as the edge where work leaves for an agent.

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

## Dropdowns

Every event and command list — trigger, publisher, fleet guard — renders one
shape, generated from the schema tree by `npm run codegen`:

    Repo · On Task Created (bloodbank.repo.task.created)
    Plane · On Ticket Created (plane.ticket.created)

`<Group> · <Label> (<value>)`: the group is the domain (or the provider, for an
alias), the label is derived from entity and action with acronyms kept upright
(CLI, LLM, PM, TTS, MCP), and the value in parentheses is exactly what the
workflow stores. A schema can override its label with a root-level
`"x-n8n-label"`. Descriptions drop the naming-contract boilerplate.

## Provider aliases

A provider alias such as `plane.ticket.created` is a filter over a canonical
event, not an event of its own: it subscribes to the canonical subject and
additionally requires `data.provider` and `data.provider_event_type` to match.

The schema is the single source. Each alias is declared on its canonical
schema's `data.provider_event_type` as an `x-provider-aliases` entry
(`{value, provider, label, description}`); codegen turns those into the
`providerAliases` table (with `canonicalType`), the trigger lists each alias
right after its canonical event, and the Plane normalizer looks its canonical
type up there instead of hardcoding it. A test fails the build if the
normalizer can emit a `provider_event_type` no schema declares.

| Alias | Filters | Declared in |
| --- | --- | --- |
| `plane.board.created` | `bloodbank.repo.board.created` | `schemas/bloodbank/repo/board.created.json` |
| `plane.ticket.created` | `bloodbank.repo.task.created` | `schemas/bloodbank/repo/task.created.json` |
| `plane.ticket.updated` | `bloodbank.repo.task.updated` | `schemas/bloodbank/repo/task.updated.json` |
| `plane.ticket.transitioned` | `bloodbank.repo.task.updated` | `schemas/bloodbank/repo/task.updated.json` |
| `plane.ticket.deleted` | `bloodbank.repo.task.updated` | `schemas/bloodbank/repo/task.updated.json` |
| `plane.ticket.commented` | `bloodbank.repo.task.appended` | `schemas/bloodbank/repo/task.appended.json` |

## Bloodbank Trigger

Choose Events to bind one or more event schemas or provider aliases. Event
delivery is always asynchronous.

**Only When Data Matches** (`dataMatch`) drops a message before it becomes an
execution unless every condition holds. A condition is a dot path into the
whole envelope and a comma list of accepted values; an empty list means
"present and non-empty", and an array matches when any element does. Saved as:

    "dataMatch": { "conditions": [
      { "path": "data.context.reason", "values": "ticket-grooming,ticket-delegation" }
    ] }

That is how a stateless workflow on `agent.invocation.started/completed/failed`
sees only ticket turns instead of every agent turn on the machine.

**Test Event Source** (`testEventSource`) decides what a manual "Test step"
emits; an active workflow always consumes live messages.

| Value | Emits |
| --- | --- |
| `replay` (default) | The newest message retained in `BLOODBANK_EVENTS` (or `BLOODBANK_COMMANDS`) that passes the bindings, alias filters and data filter — read with JetStream direct get, so no consumer of any kind is created. Falls back to a generated sample. |
| `sample` | An envelope built from the bound schema, every declared data field present, marked `"sample": true`. |
| `live` | Subscribes and waits for the next matching message (the pre-0.5.0 behaviour). |

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

### Routing

A board routes when any enrolled project claims it, agent or not:

1. **pjangler project enrollment** — every repo's `.project.json`
   `ticket_provider.board_id` (type `plane`), read from the pjangler registry
   service (`GET /v1/registry`; `PJ_PROJECT_REGISTRY`, then `PJ_REGISTRY_URL`,
   then `http://localhost:8764`, or the node's Routing › Project Registry). It
   wins on the repo slug: its `slug` becomes `data.repo`. Cached for 30 s and
   served stale if the service is down.
2. **The Hermes registry** (`HERMES_AGENTS_REGISTRY`, then
   `HERMES_FLEET_REGISTRY_FILE`, then `~/.hermes/agents-registry.yaml`) fills in
   boards pjangler does not index.
3. **Manifests of Hermes rows that name no board** — the `.project.json` at
   the row's `project_path`.

A supported event on a board none of these claim is **unrouted**, never
guessed from the workspace. Node version 2 (the default for new nodes) sends
it to a second output, **Unrouted**; version 1 keeps a single output and
answers it there (`routed: false, unrouted: true`) so a saved workflow behind
a "respond with last node" webhook keeps answering Plane. When you move a
workflow to version 2, set the Webhook node to respond immediately — with the
last-node response mode an unrouted delivery leaves the main output empty and
n8n answers 500. Events Bloodbank does not model (project updates, cycles,
modules) stay on the main output as `unsupported`.

`repo.board.created` for a board no project claims carries `repo: null`; the
workspace is always the slug (`workspace_slug`), never its UUID.

### Secrets

`op://` references resolve through `op read` (the `~/.local/bin/op` wrapper)
behind an in-process cache: one hour TTL, concurrent misses share one read, and
a failed read serves the last good value. A signature mismatch against a cached
secret forces one re-read (at most every five minutes per reference), so a
rotated secret recovers without a restart.

## 33GOD Agent Fleet

Publishes one `bloodbank.agent.invocation.start` command addressed to the fleet
agent that owns a ticket's board, on the same schema-validated transport as
everything else on the bus.

| Operation | What the agent is asked to do |
| --- | --- |
| **Groom Ticket** | Enrich one new ticket in place — labels, module, priority, cycle, exposure label, a description someone who just walked in could act on — then stamp the completion label. It is told not to split the ticket or change its state. |
| **Delegate Ticket** | Pick up a groomed ticket that reached Todo, judge its acceptance criteria, delegate the work to a worker agent, and move it to In Progress with a start date. |
| **Invoke Agent** | Send your own prompt to the agent that owns the board. |

**Outputs.** Output 0, **Dispatched**, carries one item per published command.
Output 1, **Skipped**, carries every item that was not dispatched, with a
machine-readable `code` and a readable `reason`:

| `code` | Meaning |
| --- | --- |
| `provider_event_guard` | `data.provider_event_type` is not one of Only When Provider Event Is — or is absent while the guard is set (the guard is strict) |
| `phase_guard` | Delegate Ticket: the ticket landed outside Only When Phase Is |
| `ineligible` | The owning agent exists but its registry row refuses (explicit `bloodbank.enabled: false`, wrong `gateway_scope`, `target_agent_id` mismatch, no `profile_name`) |
| `invalid_policy` | `bloodbank.enabled` is present but not a YAML boolean |
| `fenced` | The repo's `.project.json` puts execution under Krebs (`execution.mode` other than `legacy`) |
| `no_route` | No agent owns the board |
| `error` | Only with Continue On Fail: the item errored |

Every skip also publishes **`bloodbank.agent.invocation.skipped`** (schema
`schemas/bloodbank/agent/invocation.skipped.json`): `reason`, `skip_code`,
`operation`, `target_agent_id` (null when nothing was resolved), `matched_by`,
and the same `context` a start command would carry. A failed publish never
drops the item; it is recorded under `skipEvent`. Publish Skip Events turns it
off.

**Mapping.** Repository, Board ID, Ticket Key, Ticket ID, Title, Workspace,
Provider Event Type, Phase (Delegate), Correlation ID and Causation ID are
visible parameters whose defaults are expressions over the incoming envelope
(`={{ $json.data?.repo }}`, `={{ $json.correlationid }}`, `={{ $json.id }}`…),
so a Bloodbank Trigger feeds this node with no mapping at all. Clear one to fall
back to lifting it from the envelope; set one to override it.

**Registry.** There is no registry parameter: the node reads
`HERMES_AGENTS_REGISTRY`, then `HERMES_FLEET_REGISTRY_FILE`, then
`~/.hermes/agents-registry.yaml`. Workflows saved before 0.5.0 keep their
`registryFile`, `onIneligible` and `ticket` values as hidden parameters and they
still apply.

**Board id resolves the agent, not the repo slug.** The board id is the one
identifier a provider webhook always carries, and it is what tells a
multi-tenant Plane which workspace to answer as. A board with no `plane` entry in
the registry falls back to `<repo>-pm` by convention, so a project works with no
code change.

**An ineligible project is a green skip.** Eligibility mirrors hermes-gateway's
own four conditions — `profile_name`, `bloodbank.enabled`, `gateway_scope: fleet`
and a matching `target_agent_id`. No key means enabled: an absent
`bloodbank.enabled` is eligible, explicit `false` is not, and anything else
present is `invalid_policy`. A failing condition sends the item to Skipped
rather than failing the execution, so one shared trigger over every project's
tickets does not turn the switched-off ones red. The Krebs fence is checked
after eligibility; a missing or unreadable `.project.json` is legacy.

**One thread per ticket, one command per event.** Correlation comes from the
incoming envelope; without one it is `uuid5(url_ns, "plane:<board>:<ticket id>")`
— the same derivation the Plane ingress uses, so the command joins the fact
that caused it. Idempotency is separate: the command id is
`uuid5(url_ns, "agent.invocation.start:<operation>:<agent>:<causing event id>")`,
so a redelivered trigger event (and a Plane retry, whose event id is itself
deterministic) republishes the same `command_id` and `idempotency_key`, and the
gateway drops the duplicate.

### The lifecycle lane

Two versioned workflows chain through it:

    n8n import:workflow --input=../n8n-workflows/ticket-grooming.v1.json
    n8n import:workflow --input=../n8n-workflows/ticket-delegation.v1.json

`plane.ticket.created` → **Groom Ticket**, which finishes by labelling the ticket
`lifecycle:triaged`. A person then promotes the ticket to Todo, and
`plane.ticket.transitioned` → **Delegate Ticket** picks it up. The label is the
handshake between the two: grooming is automatic, promotion to Todo is the human
decision, and delegation requires both. Delegate Ticket grooms a ticket itself
when the label is missing rather than delegating unreviewed work.

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
    npm run deploy              # installs only when the build differs; restarts n8n
    npm run deploy -- --check   # exit 3 when the installed copy is not this build
    npm run deploy -- --force   # reinstall and restart regardless

The deploy identifies a build by a content hash of `dist/` plus
`package.json`, not by its version string, and verifies the installed copy
against it. It copies with `rsync --checksum`: `npm pack` stamps every file with
the same fixed mtime, so a size-and-mtime comparison skips same-length edits.

npm test covers schema generation and the shared option shape, trigger and
publisher configuration, canonical envelopes, fail-closed invocation routing,
fleet eligibility / skips / skip events / fences / idempotent command ids,
Plane creation/transition/comment normalization against the full schemas,
schema-declared provider aliases, merged board routing, the secret cache,
data filters, JetStream replay and generated samples, and the node icon
contract. npm run test:live proves multi-event subscriptions, command
queue competition, and synchronous command replies against the live NATS
service.
