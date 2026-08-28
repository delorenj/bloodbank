# Plane webhook compatibility relay (retired)

Status: source retained for emergency rollback; not installed or active.

The canonical Plane ingress is the versioned n8n workflow at
/webhook/plane. It verifies Plane's HMAC, normalizes provider payloads, and
publishes schema-backed Bloodbank events.

This service preserved the historical `:8477/plane-webhook` URL during the
migration. It does not build or publish events. It verifies the same HMAC and
relays the original bytes and signature to n8n:

    Plane -> compatibility relay -> n8n Webhook -> Plane to Bloodbank node
                                               -> bloodbank.evt.repo.*

All live Plane webhooks now call `https://n8n.delo.sh/webhook/plane` directly
with per-webhook HMAC secrets. Do not install this relay for normal operation.

## Event normalization

| Plane provenance | Canonical CloudEvent | n8n trigger label |
| --- | --- | --- |
| plane.board.created | bloodbank.repo.board.created | On Board Created |
| plane.ticket.created | bloodbank.repo.task.created | On Ticket Created |
| plane.ticket.updated | bloodbank.repo.task.updated | On Ticket Updated |
| plane.ticket.transitioned | bloodbank.repo.task.updated | On Ticket Transitioned |
| plane.ticket.commented | bloodbank.repo.task.appended | On Ticket Commented |
| plane.ticket.deleted | bloodbank.repo.task.updated | On Ticket Deleted |

Provider names intentionally remain in data.provider_event_type and the n8n
binding alias. Bloodbank wire types remain provider-neutral per the event naming
contract. Every normalized envelope carries workspace, board_id, slug, and
provider_event_type extensions; its data carries the same routing metadata plus
the lossless ticket, board, or comment JSON entity.

## Rollback only

The Python source and unit file are retained so the previous behavior remains
auditable. Any temporary rollback must use an `op://` secret reference and must
not replace the canonical HTTPS ingress in Plane.
