# Plane webhook normalization

Status: active
Owner: Bloodbank integration surface
Ingress: n8n workflow Plane to Bloodbank

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

## Mapping

| Plane webhook | Provenance name | Bloodbank type | Bloodbank subject |
| --- | --- | --- | --- |
| project / created | plane.board.created | bloodbank.v1.repo.board.created | bloodbank.evt.v1.repo.board.created |
| issue / created | plane.ticket.created | bloodbank.v1.repo.task.created | bloodbank.evt.v1.repo.task.created |
| issue / updated | plane.ticket.updated | bloodbank.v1.repo.task.updated | bloodbank.evt.v1.repo.task.updated |
| issue / updated with state activity | plane.ticket.transitioned | bloodbank.v1.repo.task.updated | bloodbank.evt.v1.repo.task.updated |
| issue_comment / created | plane.ticket.commented | bloodbank.v1.repo.task.appended | bloodbank.evt.v1.repo.task.appended |
| issue / deleted | plane.ticket.deleted | bloodbank.v1.repo.task.updated | bloodbank.evt.v1.repo.task.updated |

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

## Compatibility relay

The historical port-8477 Plane bridge is now a byte-preserving relay to n8n.
It cannot construct Bloodbank envelopes. This keeps old Plane webhook
configuration functional without maintaining a second normalizer or event
dialect.
