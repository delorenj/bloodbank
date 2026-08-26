# Plane webhook compatibility relay

The canonical Plane ingress is the versioned n8n workflow at
/webhook/plane. It verifies Plane's HMAC, normalizes provider payloads, and
publishes schema-backed Bloodbank events.

This service preserves the historical :8477/plane-webhook URL while Plane
configuration is migrated. It does not build or publish events. It verifies the
same HMAC and relays the original bytes and signature to n8n:

    Plane -> compatibility relay -> n8n Webhook -> Plane to Bloodbank node
                                               -> bloodbank.evt.v1.repo.*

Direct and relayed requests therefore produce the same n8n provenance and the
same canonical event dialect.

## Event normalization

| Plane provenance | Canonical CloudEvent | n8n trigger label |
| --- | --- | --- |
| plane.board.created | bloodbank.v1.repo.board.created | On Board Created |
| plane.ticket.created | bloodbank.v1.repo.task.created | On Ticket Created |
| plane.ticket.updated | bloodbank.v1.repo.task.updated | On Ticket Updated |
| plane.ticket.transitioned | bloodbank.v1.repo.task.updated | On Ticket Transitioned |
| plane.ticket.commented | bloodbank.v1.repo.task.appended | On Ticket Commented |
| plane.ticket.deleted | bloodbank.v1.repo.task.updated | On Ticket Deleted |

Provider names intentionally remain in data.provider_event_type and the n8n
binding alias. Bloodbank wire types remain provider-neutral per the event naming
contract. Every normalized envelope carries workspace, board_id, slug, and
provider_event_type extensions; its data carries the same routing metadata plus
the lossless ticket, board, or comment JSON entity.

## Runtime

The signing secret is an op:// reference in
~/.hermes/plane-webhook-bridge.env. The systemd unit resolves it in memory with
op run; plaintext secret files are not supported.

    systemctl --user daemon-reload
    systemctl --user restart hermes-plane-webhook-bridge.service
    curl -fsS http://localhost:8477/health
    op run --env-file="$HOME/.hermes/plane-webhook-bridge.env" -- \
      python services/plane-webhook-bridge/plane-webhook-bridge.py --selftest

The default relay target is http://localhost:5678/webhook/plane. Override it
with N8N_PLANE_WEBHOOK_URL when n8n runs under another service hostname.
