# Cost observation delivery

The collector posts only `bloodbank.billing.cost.observed` to
`https://cost-events.delo.sh/v1/cost-observations`. Its scoped bearer must match
both payload client and project. The ingress saves the complete original
CloudEvent to SQLite with FULL synchronous commits, publishes to the existing
`BLOODBANK_EVENTS` stream and returns success only after its durable PubAck.
It never modifies the global stream or calls a phone workflow.

The durable consumer `portal-cost-observations-v1` ACKs after the portal confirms
that exact event ID was committed. Refusals and outages are retried; `/healthz`
shows archive, backlog and error counts. The permanent `cost-archive` volume
must be backed up with the platform's persistent volumes. Never delete it on
a container upgrade. Original envelopes, including IDs and timestamps, remain
archived after delivery and after the broker's seven-day retention expires.

Run from this directory with credentials resolved only in process memory:

```sh
op run --env-file .env.op -- docker compose --env-file /dev/null up -d --build
```

After an outage longer than the broker retention, replay the archived month:

```sh
op run --env-file .env.op -- docker compose --env-file /dev/null exec cost-bridge \
  python /app/services/cost-bridge/main.py replay --project PROJECT_ID --month 2026-09
```

The replay retains every original ID and revision. D1 deduplication remains
valid outside NATS's short dedupe window. It refuses conflicting identities;
a later vendor correction must have a new revision. For additional real
clients set `COST_SCOPES_JSON` to a vault-held JSON array of mappings containing
`client_id`, `project_id`, `ingress_token`, `portal_token` and `portal_url`.
Compose passes this map through unchanged; the singleton fields remain supported
for the current project. The second fixture client stays in local tests and is
never provisioned here.

Invalid broker messages receive a durable private quarantine record and terminal
disposition. Their count remains visible in readiness; malformed payloads cannot
occupy the consumer indefinitely. Transient portal failures retain the original
valid envelope and are retried until the matching persistence receipt arrives.
