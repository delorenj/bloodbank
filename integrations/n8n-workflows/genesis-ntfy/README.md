# Genesis -> ntfy

n8n workflow `sr604yqfOwRgdmiZ` ("Genesis -> ntfy", active). Every `bloodbank.genesis.*` fact becomes a push on ntfy topic `genesis`.

```
Bloodbank Trigger "Genesis event" (durable, 24h catch-up, ack after execution)
  -> Code "Format ntfy" (format.js)  -> HTTP Request "Publish to ntfy" (JSON publish to https://ntfy.delo.sh/)
```

| Event | Push |
| --- | --- |
| `bloodbank.genesis.approval.requested` | **Approval needed · <client_name>**, priority 4, markdown: summary, question, recipients, subject, quoted `body_preview`, expiry, channel. Tap or "Open operator" goes to `data.operator_url`. `sequence_id` = `approval_id`. |
| `bloodbank.genesis.approval.granted` / `denied` / `expired` | Same `sequence_id`, so ntfy replaces the request in place: **Approved / Declined / Expired · <client_name>**, priority 2. |
| `bloodbank.genesis.alert.raised` | **Genesis alert · <client_name>**, reason, summary, detail, priority 4 (5 when `severity` is `critical`). `sequence_id` = `alert_id`. |

`data.test: true` prefixes the title with `[test]` and adds the `test_tube` tag.

No Schedule, Cron or Interval node: the trigger is a durable JetStream consumer. The HTTP node retries up to 3 times inside one execution. The ntfy token is the existing n8n credential `ntfy phone publisher (n8n user)` (`Ek5QjwyMWTolNgcO`, user `n8n`, write-only on `genesis`); nothing inline.

`services/ntfy-router` mutes `bloodbank.genesis.*`, otherwise it would mirror each event to the `genesis` topic a second time.

## Redeploy

```bash
./deploy.sh      # build.mjs -> workflow.json, run tests, create or PUT by name, activate, read back
```

Edit `format.js` (the wording) or `build.mjs` (the graph); `workflow.json` is generated and committed. Do not edit `~/code/delonet-n8n-workflows/workflows`: that is an export of what the API holds. Unit tests: `node --test test/format.test.mjs`.

## Prove it

`python3 smoke.py` publishes one synthetic event per type (`data.test: true`, client `test`) through JetStream with `Nats-Msg-Id` = envelope id. It buzzes the phone five times; run it once. Then check Candystore (`curl 'http://127.0.0.1:8683/events?producer=genesis&limit=10'`), the workflow's executions, and `GET https://ntfy.delo.sh/genesis/json?poll=1&since=15m`.
