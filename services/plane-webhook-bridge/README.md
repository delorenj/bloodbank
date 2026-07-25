# plane-webhook-bridge

Real-time PM board reactivity: self-hosted **Plane** webhooks → repo-scoped
**Bloodbank** events → the right PM reacts on **its** board.

```
Plane (docker) ──webhook──▶ bridge ──NATS──▶ bloodbank.evt.v1.repo.<repo>.ticket_*
                                              └▶ PM's bloodbank-consumer (already
                                                 subscribes …repo.<repo>.>) ─▶ inbox
                                                 ─▶ gateway turn ─▶ PM reads + reacts
```

- **Routing** keys off the agents-registry: `agents.<id>.plane.project_id → repo`.
  The emitted type `bloodbank.v1.repo.<repo>.ticket_<action>` is 5-token
  contract-compliant AND matches each PM's existing subscription.
- **One** fleet service, **one** Plane workspace webhook — it fans every
  project's events to the correct PM. Cares about `issue` + `issue_comment`
  events; acks-and-ignores the rest.

## Deploy

```bash
# 1. secret (optional but recommended — enables HMAC verify)
echo 'PLANE_WEBHOOK_SECRET=<random-hex>' > ~/.hermes/plane-webhook-bridge.env
mkdir -p ~/.hermes/logs

# 2. install + start the systemd user service
cp hermes-plane-webhook-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-plane-webhook-bridge.service
curl -s localhost:8477/health          # {"ok":true,"projects":20}

# 3. dry-check routing without NATS/Plane
~/.hermes/hermes-agent/.venv/bin/python plane-webhook-bridge.py --selftest
```

## Configure Plane (self-hosted)

Create ONE **workspace webhook** pointing at the bridge (Plane's containers reach
the host via `host.docker.internal`):

- **URL:** `http://host.docker.internal:8477/plane-webhook`
- **Secret:** the same `PLANE_WEBHOOK_SECRET`
- **Events:** Issues (+ Issue Comments)

Either via **Workspace Settings → Webhooks** in the Plane admin UI, or the API:

```bash
curl -X POST "$PLANE_API_URL/api/workspaces/33god/webhooks/" \
  -H "X-API-Key: $PLANE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"url":"http://host.docker.internal:8477/plane-webhook","is_active":true,
       "secret_key":"'"$PLANE_WEBHOOK_SECRET"'","issue":true,"issue_comment":true}'
```

## What the PM does on an event

The PM's sentinel pass (`.scripts/sentinel.prompt.md`) now has a **Trigger** note
(read the changed ticket first, react) and a **Before-going-idle protocol**:
(a) claim any unclaimed ready/unstarted ticket (delegation cycle), and
(b) status-sweep every in-progress ticket — post a status note, and actively
unblock anything blocked. So a board change wakes the PM immediately, and every
pass ends by clearing claimable work + chasing in-flight tickets.
