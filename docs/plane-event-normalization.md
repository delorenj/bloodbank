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
  -> NATS bloodbank.evt.v1.repo.*
  -> Candystore durable event projection
```

TLS encrypts the request in transit. HMAC authenticates the exact raw request
body; it is not itself encryption. Secret values remain concealed in 1Password
and are resolved only at n8n execution time. An unknown webhook ID or invalid
signature is rejected before publication.

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

Project identity is declared in each repo's `.project.json` and reconciled into
the shared Hermes `~/.hermes/agents-registry.yaml`. The n8n node reads that
fleet registry on every execution so a reconciled board migration is live
without redeploying the node. Unknown board IDs are acknowledged as unrouted;
workspace-only guessing is forbidden because one Plane workspace can own many
boards.

## Mapping

| Plane webhook | Provenance name | Bloodbank type | Bloodbank subject |
| --- | --- | --- | --- |
| project / create | plane.board.created | bloodbank.v1.repo.board.created | bloodbank.evt.v1.repo.board.created |
| issue / create | plane.ticket.created | bloodbank.v1.repo.task.created | bloodbank.evt.v1.repo.task.created |
| issue / update | plane.ticket.updated | bloodbank.v1.repo.task.updated | bloodbank.evt.v1.repo.task.updated |
| issue / update with state activity | plane.ticket.transitioned | bloodbank.v1.repo.task.updated | bloodbank.evt.v1.repo.task.updated |
| issue_comment / create | plane.ticket.commented | bloodbank.v1.repo.task.appended | bloodbank.evt.v1.repo.task.appended |
| issue / delete | plane.ticket.deleted | bloodbank.v1.repo.task.updated | bloodbank.evt.v1.repo.task.updated |

The Plane provenance name lives in data.provider_event_type and is exposed as a
first-class n8n trigger alias, such as On Ticket Created. It does not become a
CloudEvents type token: the naming contract requires provider-neutral wire
facts. Trigger aliases subscribe to the canonical subject and filter the
provider provenance inside the envelope.

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

## Idempotency and ordering

The n8n ingress derives a deterministic event UUID from provider event name,
board, entity, source timestamp, and state/comment identity. A webhook retry
therefore republishes the same event ID. Ordering keys are board:<board_id> for
board events and task:<repo>:<ticket_id> for ticket events.

## Retired compatibility relay

The historical port-8477 Plane bridge is disabled and uninstalled from the user
systemd runtime. Its source remains only as rollback material. No active Plane
webhook depends on a LAN address, port 8477, or the retired single shared secret.
