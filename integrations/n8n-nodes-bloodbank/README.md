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

**Delivery** (`delivery`, events only) decides what an *active* workflow does
about events published while it is not listening — during an n8n restart, a
deploy, or the gap every save opens (n8n re-registers all of a workflow's
triggers on any save, an API `PUT` that only changes pinData included).

| Value | Behaviour |
| --- | --- |
| `durable` (default) | A JetStream durable pull consumer on `BLOODBANK_EVENTS`, one per (workflow, node), named `n8n-<workflow id>-<node id>`. Filter subjects are the bound subjects. Created with `deliver_policy: new`, so a first activation starts at the newest event rather than replaying the stream's 7 days. Deactivating or restarting keeps the durable, so the next activation resumes exactly where the last one stopped. `inactive_threshold` is 7 days: the durable of a workflow that never comes back deletes itself. |
| `ephemeral` | A core NATS subscription (the pre-0.6.0 behaviour). Hears only what is published while it is open. |

`BLOODBANK_EVENTS` uses `limits` retention, so a durable changes nothing about
what the stream keeps; it only remembers this trigger's position. With a durable:

- Messages are pulled **one at a time** and **Acknowledge** (`acknowledge`)
  decides when each is acked. `afterExecution` (default) acks when the
  execution it started has finished — success or error — so a trigger's
  executions run in stream order. `onEmit` acks as soon as the execution starts
  and lets executions overlap; use it for long-running workflows.
- A message a trigger is not bound to (another alias of the same subject, a
  failed data filter) is acked without an execution. A malformed one is
  terminated and recorded as a failed execution.
- While an execution runs, the message is kept alive with `working()` (ack wait
  30 s); after 5 minutes the trigger acks anyway and moves on.
- **Catch-Up Window (Hours)** (`catchUpHours`, default 24) skips — acks without an
  execution — anything older than the window when it is delivered, so re-enabling
  a workflow after a long break does not replay days of history. `0` = no limit.
- Manual test runs never touch the durable: Replay and Sample read without a
  consumer, and Wait for Live uses a throwaway subscription.

Inspect the durables with `nats consumer ls BLOODBANK_EVENTS` (or
`curl -s 'localhost:8222/jsz?consumers=true'`); delete one with
`nats consumer rm BLOODBANK_EVENTS n8n-<workflow>-<node>` to make the next
activation start fresh from the tip.

**Proven live on 2026-09-23 (0.6.0).** *Re-activation gap:* Ticket Grooming was
deactivated, smoke ticket 33GOD-67 was created (Plane → Bloodbank execution
241014 published the fact; the durable showed `pending 1`), and on re-activation
Grooming execution 241015 dispatched it (`invoked: true`). *n8n stopped:* once
chip execution 241016 had added `agent:working`, n8n was stopped with
`pm2 stop n8n`. 33god-pm's turn ended while n8n was down
(`invocation.completed` 6bfd5f83… at 04:46:06Z, the chip still on the ticket).
After `pm2 start n8n`, chip execution 241018 consumed that event and removed the
chip seven seconds after startup.

**What a durable cannot cover.** The Plane webhook is received by n8n itself
(*Plane → Bloodbank*). While n8n is down, Plane's delivery gets a 502 from
Traefik and Plane does not retry an HTTP error (it retries only connection
failures), so a ticket event that happens during an n8n outage never reaches the
bus at all. Durable triggers protect everything that *is* on the bus — agent turn
events, facts published by other services, and every fact published while a
workflow is being re-saved.

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

Command triggers keep their core NATS queue-group subscription: commands already
live in the work-queue stream `BLOODBANK_COMMANDS`, and the request/reply path
needs the transport reply subject.

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
deterministic) republishes the same `command_id` and `idempotency_key`. The
command's `time` is the causing event's `time` (**Observed At**, default
`={{ $json.time }}`), which makes the whole rebuilt envelope byte-identical.
That matters because the hermes gateway journals a command under its
`command_id` *and a sha256 of the entire envelope*: an identical replay is
recognised as the command it already has (still running: nak and retry later;
finished: re-publish the journaled started/terminal events and ack, no second
turn), while the same `command_id` with a different digest — which a wall-clock
`time` used to produce — is terminally rejected as a collision. Commands are also
published with `Nats-Msg-Id: <command_id>`, so `BLOODBANK_COMMANDS` drops a
republished duplicate inside its 2-minute duplicate window before the gateway
even sees it.

### The lifecycle lane

Four versioned workflows in `../n8n-workflows/` carry a Plane ticket from
webhook to working agent. Import all four; each export records `active: true`.

    for f in plane-bloodbank ticket-grooming ticket-delegation ticket-pickup-chip; do
      n8n import:workflow --input=../n8n-workflows/$f.v1.json
    done

| Workflow (id) | Starts on | Does | Pushes to ntfy `lifecycle` |
| --- | --- | --- | --- |
| **Plane → Bloodbank** (`iMw484J1ZCqKME2C`) | Plane webhook | Verifies, normalizes and publishes the `bloodbank.repo.*` fact (see [Plane ingress](#plane-ingress)) | **Unrouted** → *Unrouted — Once a Day* → *Unrouted Board*: a board no enrolled project claims, at most once per board per 24 h |
| **Ticket Grooming** (`6wAGA5pdrmHLyhs2`) | `plane.ticket.created` | **Groom Ticket** commands the board's agent to enrich the ticket and stamp `lifecycle:triaged` | **Dispatched** → *Triage Started*; **Skipped** → *Triage Skipped*, every skip |
| **Ticket Delegation** (`8mmqdMwQYA28ZwUj`) | `plane.ticket.transitioned` | **Delegate Ticket** (phase guard `Todo,unstarted`) commands the agent to pick the ticket up and delegate it | **Dispatched** → *Delegation Started*; **Skipped** → *Notable Skip?* → *Delegation Skipped* |
| **Ticket Pickup Chip** (`wWXgCZiiIBWaRRzE`) | One trigger, *Invocation Lifecycle*, on `agent.invocation.started`, `.completed`, `.failed` with `data.context.reason` in `ticket-grooming,ticket-delegation`; plus *Stale Chip Sweep* hourly | Adds the board's `agent:working` label while the agent's turn runs and removes it when the turn ends; the sweep removes any chip left on a ticket whose last turn ended | none |

**The handshake.** Grooming finishes by labelling the ticket
`lifecycle:triaged`. A person then promotes it to Todo, and that transition is
what Delegate Ticket picks up. Grooming is automatic, promotion to Todo is the
human decision, and delegation requires both: Delegate Ticket grooms a ticket
itself when the label is missing rather than delegating unreviewed work.

**No skip is silent.** Every item a Fleet node does not dispatch leaves on
**Skipped** with its `code` and `reason` (see [33GOD Agent Fleet](#33god-agent-fleet)),
and — with Publish Skip Events on in both lanes — is also published as
**`bloodbank.agent.invocation.skipped`**, so Candystore holds the durable record
and anything on the bus can react to it. The ntfy push is the page on top of that
record. Grooming pages every skip. Delegation pages every skip except
`phase_guard` and `provider_event_guard`: a transition into anything other than
Todo is routine, so it is published but does not page. An unrouted Plane board is
different: nothing reaches the bus for it, so the *Unrouted Board* push is its
only signal, and it names the board to add to a project's `.project.json`. It
pages once per board per 24 hours: *Unrouted — Once a Day* keeps the last page
time per board in workflow static data and marks later deliveries
`notify: false`, which end on *Unrouted — Muted* (the webhook answers with the
last node's item, so the muted path must still end on one).

**A failed push never fails a lane.** Every ntfy node runs with On Error =
Continue, so the execution stays green and the failure is visible only as an
`error` on that node's output — check there first when pages stop arriving. The
pushes authenticate as the ntfy user `n8n` (credential *Ntfy account (n8n
token)*), which sits on ntfy's `service` tier and so has its own request limit.
Until 2026-09-22 it had no tier and shared a per-IP limit with the Bloodbank
event toaster, which answered most lane pushes with 429.

**The chip is stateless.** Everything it needs rides on the gateway's echoed
`data.context` (`workspace`, `board_id`, `ticket_id`, `ticket_key`, `reason`):
no static data and no board map. It resolves `agent:working` by name on each
board and skips boards without one; boards listed in the n8n env var
`KREBS_FENCED_BOARDS` (a JSON array of board ids) are Krebs's to chip. Its three
Plane calls (List Labels, Read Issue, Write Labels) run with On Error =
Continue, and the Code node after each checks the result: 404 or 410 means the
ticket or board was deleted while the turn was running, so there is nothing to
chip and the item is dropped with the execution green. Any other failure (an
expired Plane token, a 5xx) fails the execution with the ticket key in the
message.

**The chip is ordered and swept.** Started and ended arrive on ONE trigger, so
they share one durable and one queue: a turn's `started` execution always
finishes before its `completed` one starts, even when both land in the same
catch-up burst after a restart (two triggers would be two durables with no
order between them, and the add could land after the remove). *Stale Chip
Sweep* runs hourly at minute 51 (pinned; left unset, n8n picks a random minute
on every activation): it asks Candystore for the gateway's invocation events of
the last 48 h, and for every ticket whose latest one is a `completed`/`failed`
at least 10 minutes old it sends that event down the chip line as a remove. The
line only writes when `agent:working` is actually still on the ticket **and the
ticket has not changed since that turn ended** (its `updated_at` is no later
than the ending event's `time` plus 5 s). `agent:working` is also pilot's claim
marker (`px claim` adds it, `px close` removes it) and the label cannot say who
put it there, so a ticket a worker claimed after the PM's turn keeps it; so does
any ticket edited since, and any ticket whose times cannot be read. The live
lane is unchanged: it removes at the real turn end. The sweep is the net under a
failed chip write (a Plane 5xx), an outage longer than the catch-up window, or
an expired durable; a chip that got stuck on a ticket someone then edited has to
come off by hand. Candystore is read at `$CANDYSTORE_URL`
(default `http://127.0.0.1:8683`; the public host sits behind Google OIDC).

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
the lifecycle lane's wiring as exported (skip and unrouted pushes, the
once-a-day unrouted gate, the chip's single ordered trigger, its Code nodes, its
deleted-ticket guards and the stale-chip sweep), durable trigger delivery
(consumer naming and config, create/update/resume, one-at-a-time ack after the
execution, catch-up window, close semantics) against a fake transport,
byte-identical re-dispatch and the command `Nats-Msg-Id`,
Plane creation/transition/comment normalization against the full schemas,
schema-declared provider aliases, merged board routing, the secret cache,
data filters, JetStream replay and generated samples, and the node icon
contract. npm run test:live proves multi-event subscriptions, command
queue competition, and synchronous command replies against the live NATS
service.
