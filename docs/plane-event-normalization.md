# Plane webhook normalization

Status: active
Owner: Bloodbank integration surface
Ingress: n8n workflow Plane to Bloodbank

## Live connection

Both Plane workspaces use the same public HTTPS ingress and the same active n8n
workflow. Each Plane webhook has its own secret and is selected by the signed
payload's `webhook_id`:

| Plane workspace | Webhook ID | Secret reference |
| --- | --- | --- |
| `33god` | `24bc401a-00fa-46cd-bfff-65e14ca1707a` | `op://DeLoSecrets/PlaneWebhook-33GOD/credential` |
| `automaticai` | `4eb4732b-6005-4c9d-ac6f-7e643470768e` | `op://DeLoSecrets/PlaneWebhook-AutomaticAI/credential` |

The workspace name `automaticai` is only a tenant name inside the self-hosted
Plane instance. It is not a separate service, company boundary, or ingress.

```text
Plane (33god or automaticai workspace)
  -> HTTPS POST https://n8n.delo.sh/webhook/plane
  -> n8n workflow Plane -> Bloodbank (iMw484J1ZCqKME2C)
  -> custom Plane -> Bloodbank node
       1. select secret by webhook_id
       2. verify X-Plane-Signature over the raw body
       3. normalize Plane create/update/delete actions
       4. publish one canonical event
  -> NATS bloodbank.evt.repo.*
  -> Candystore durable event projection
```

TLS encrypts the request in transit. HMAC authenticates the exact raw request
body; it is not itself encryption. Secret values remain concealed in 1Password.
The node resolves each `op://` reference once and caches it in-process for an
hour (serving the last good value if 1Password is unreachable, and re-reading
once on a signature mismatch so a rotated secret recovers); resolving on every
delivery is what let 1Password rate limits drop tickets on 2026-09-20. An
unknown webhook ID or invalid signature is rejected before publication.

## Boundary

n8n is the canonical provenance boundary for Plane webhooks. A Plane request is
accepted only after its HMAC is verified from the raw request body. The
normalizer then publishes one provider-neutral Bloodbank fact.

The event envelope records:

- source: urn:33god:integration:n8n:plane-webhook
- producer: n8n-plane-webhook
- service: n8n
- actor.provider: plane
- workspace, board_id, slug, and provider_event_type extension attributes

The payload repeats the routing fields and preserves the provider entity as
lossless JSON under ticket, board, or comment.

## Routing source of truth

Project identity is declared in each repo's `.project.json`
(`ticket_provider.board_id`). A board is routable as soon as an enrolled
project claims it — whether or not that project has an agent. The node builds
its board table from, in order of authority:

1. **pjangler project enrollment** — the pjangler registry service
   (`GET http://localhost:8764/v1/registry`, or `PJ_PROJECT_REGISTRY` /
   `PJ_REGISTRY_URL`), which indexes every repo's `.project.json`. It wins on the
   repo slug: its `slug` becomes `data.repo`.
2. **The Hermes org chart** (`~/.hermes/agents-registry.yaml`) for boards
   pjangler does not index.
3. **The `.project.json` of a Hermes row that names no board**, read directly.

Both registries are read on every execution (pjangler through a 30-second
cache that serves stale if the service is down), so a board migration is live
without redeploying the node. Workspace-only guessing is forbidden because one
Plane workspace can own many boards.

A supported event on a board nothing claims is **unrouted**. That is an
enrollment gap, not a no-op: node version 2 sends it to a dedicated
**Unrouted** output (version 1 answers it on its single output as
`routed: false, unrouted: true`). Events Bloodbank does not model — project
updates, cycles, modules — are `unsupported` and stay on the main output.

## Mapping

The schema is the single source for these aliases. Each one is declared on its
canonical schema as an `x-provider-aliases` entry on
`data.provider_event_type` (`{value, provider, label, description}`); the n8n
package generates its trigger aliases and the normalizer's canonical-type
lookup from those declarations, and a test fails the build if the normalizer
can emit a provenance name no schema declares. To add or rename one, edit the
schema — not this table, and not the node.

| Plane webhook | Provenance name (`x-provider-aliases`) | Declared in | Bloodbank type |
| --- | --- | --- | --- |
| project / create | plane.board.created | `schemas/bloodbank/repo/board.created.json` | bloodbank.repo.board.created |
| issue / create | plane.ticket.created | `schemas/bloodbank/repo/task.created.json` | bloodbank.repo.task.created |
| issue / update | plane.ticket.updated | `schemas/bloodbank/repo/task.updated.json` | bloodbank.repo.task.updated |
| issue / update with state activity | plane.ticket.transitioned | `schemas/bloodbank/repo/task.updated.json` | bloodbank.repo.task.updated |
| issue / delete | plane.ticket.deleted | `schemas/bloodbank/repo/task.updated.json` | bloodbank.repo.task.updated |
| issue_comment / create | plane.ticket.commented | `schemas/bloodbank/repo/task.appended.json` | bloodbank.repo.task.appended |

The subject is always `bloodbank.evt.<domain>.<entity>.<action>` of the
canonical type. The Plane provenance name lives in data.provider_event_type and
is exposed as a first-class n8n trigger alias, rendered
`Plane · On Ticket Created (plane.ticket.created)` right after its canonical
event. It does not become a CloudEvents type token: the naming contract
requires provider-neutral wire facts. Trigger aliases subscribe to the
canonical subject and filter the provider provenance inside the envelope.

`repo.task.created` requires `provider`, `provider_event_type`, `board_id` and
`ticket_id` in addition to `repo` and `title`: it is a fact a ticket provider
observed, and those are the fields every consumer keys on.

## Krebs lifecycle projection

These events are the Bloodbank schema implementation of the Krebs provider
mapping:

- ticket creation becomes repo.task.created
- ticket changes and state transitions become repo.task.updated
- ticket comments become repo.task.appended

The five provider-portable ticket bands are backlog, unstarted, started,
in_review, and completed. Plane state group or state_type is normalized into
that vocabulary when the webhook includes it. The raw entity remains available
when Plane provides only an opaque state ID.

Project board creation becomes repo.board.created. This is the event Pjangler
can consume after creating a Plane board; board_id and slug are both present.
When no enrolled project claims the board yet, `repo` is `null` rather than a
slug guessed from the board name, and `workspace` is the workspace slug from
the delivery (`workspace_slug`), never its UUID.

## Idempotency and ordering

The n8n ingress derives a deterministic event UUID from provider event name,
board, entity, source timestamp, and state/comment identity. A webhook retry
therefore republishes the same event ID. Ordering keys are board:<board_id> for
board events and task:<repo>:<ticket_id> for ticket events.

## Downstream: fleet dispatch

The Ticket Grooming and Ticket Delegation workflows feed these facts to the
33GOD Agent Fleet node, which dispatches `bloodbank.agent.invocation.start` to
the board's agent or, when it does not, publishes
`bloodbank.agent.invocation.skipped` with the reason (guard miss, ineligible,
invalid policy, Krebs fence, no route). A ticket nobody picked up is therefore
explainable from the bus alone.
