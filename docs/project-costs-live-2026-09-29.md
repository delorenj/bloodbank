# Project cost pipeline: deployed proof and operations

The independent collector, archived Bloodbank ingress and portal cost ledger
are deployed for James Brennan. The current-month backfill completed and the
six-hour schedule is enabled. This path never calls Relay, Voice, GorillaDesk
or the phone release controller. No draft, statement, invoice, notification or
synthetic production client was created during deployment.

## Deployed identities

| Component | Verified artifact or resource |
| --- | --- |
| Portal source | `AutomaticAI-io/client-portal` main `709cce9fe216de27be8f2af9ef4aca431750db60` |
| Portal Worker | `automatic-ai`, version `63eb05f2-25f6-4bb3-aedb-c3146cb801d0` |
| Portal migration | Additive `migrations/d1/0004_project_costs.sql` |
| Bloodbank source | `delorenj/bloodbank` implementation `211e6aef5cd45a1f2aa62be1a143845c1617937b` |
| Bridge running image | `sha256:d4a5f580e440ca30131a2f4d8330e516bc23c16fe3f5f28122b25a5f101947a5` |
| Bridge archive | Docker volume `bloodbank-cost-bridge_cost-archive` |
| Collector image | `067200612963.dkr.ecr.us-east-1.amazonaws.com/automaticai/project-cost-collector@sha256:22dbc2c5ff3816a2dd70dcad28e88c8a6ec74843042879a606a16e9c2932fb59` |
| Collector task definition | `arn:aws:ecs:us-east-1:067200612963:task-definition/automaticai-project-cost-collector:3` |
| Successful September backfill | ECS cluster `james-brennan-relay`, task `4b9e8a956f6948a7a30e94212008b3fc`, exit 0 |
| Enabled schedule | `automaticai-project-costs-james-brennan`, `rate(6 hours)`, exact task definition above |
| Source/event archive | Versioned bucket `automaticai-project-costs-067200612963`, prefix `projects/9f1c1d4e-0a1b-4c2d-8e3f-000000000002/` |
| Collector network | Outbound HTTPS only, security group `sg-074f9a6a16189e41b`; no inbound rules |

The portal's `/api/health` returned that exact source SHA and a successful D1
check after deployment and cache purge. Its workflow runs only on explicit
`workflow_dispatch` from main, after tests, type checking and a strict build.
Main pushes do not deploy it automatically.

The only live client/project mapping is client
`9f1c1d4e-0a1b-4c2d-8e3f-000000000001`, project
`9f1c1d4e-0a1b-4c2d-8e3f-000000000002`. Machine credentials enforce both.

## Live acceptance

Three real current-month collections retained 36 original observations. The
final read-only verification found all 36 in D1 and in the bridge archive,
zero pending publication, zero pending projection, zero delivery errors,
an empty S3 outbox and zero statements. Four monetary source documents were
read back from S3 and their SHA-256 digests matched the ledger evidence.

| Provider | Latest observed USD at verification | Treatment |
| --- | ---: | --- |
| AWS | 127.582711 | Vendor estimate, shared account, unallocated |
| Deepgram | 220.993090 | Actual key-scoped expense; September includes historical staging |
| Twilio | 21.165160 | Actual subaccount expense; September includes historical staging |
| OpenRouter | 6.007537 | Actual monthly key usage; LLM expense excluded from rebilling |

These are dated observations, not frozen future totals. All four remain
private and excluded from billable September totals. AWS's earlier unavailable
actual reading remains separate from the known estimate: 12 providers have
13 effective measurement keys. Cloudflare, Cartesia, Resend, Clerk, Langfuse,
PostHog, GorillaDesk and Stripe explicitly retain unknown billing coverage.
Cartesia credits do not establish a dollar amount. No shared allocation was
invented. Manual invoices or allocations require a retrievable source excerpt
and the authenticated operator's recorded identity.

## Repeatable validation

In `client-portal`:

```sh
node_modules/.bin/vitest run
node_modules/.bin/tsc --noEmit
STRICT_PROD_CREDENTIALS=1 CLOUDFLARE_API_TOKEN= node_modules/.bin/vite build
python3 -m unittest discover -s costs/tests -v
node_modules/.bin/tsx costs/rehearse.ts
```

The final gate passed 507 portal tests across 39 files, type checking, strict
build and seven collector tests. The isolated rehearsal uses real HTTP,
JetStream, the durable subscriber and the actual D1 adapter. Two fixture
clients generate 24 observations; stream deletion followed by archive replay
restores all 24 with no duplicate rows. Wrong credentials and tenant bindings
are rejected. The rehearsal also checks the portal's vendored schema against
Bloodbank's canonical contract.

Bloodbank passed three bridge tests and five event-validator tests. Bridge
tests cover persistence before PubAck, restart/replay, token/scope refusal,
signed credits, timestamp offsets and acknowledgement only after the portal
confirms its commit. Portal tests use actual session/membership/operator gates
and SQLite transactions for authorization, manual/API revision interleaving,
source authorship, negative credits and immutable published statements. No
live client login or publication was exercised as a test.

## Operating references

The portal owns `costs/collector.py`, `costs/projects/james-brennan.json`,
`costs/provision_aws.py`, `costs/enable_schedule.py`, `costs/provision_portal.py`
and the task/schedule templates under `costs/ecs/`. Its `costs/README.md`
documents collection, evidence, retries and replay. Provisioning always leaves
the schedule disabled. The enable command requires a successful backfill and
refuses if the schedule points at a different task definition.

Bloodbank owns `services/cost-bridge/compose.yml`, its Dockerfile, `.env.op`,
the persistent archive and `schemas/bloodbank/billing/cost.observed.json`.
Run the compose command in that service's README through `op run`; never
resolve credentials into a file. The ingress is
`https://cost-events.delo.sh/v1/cost-observations`; the portal receiver is
`https://automaticai.io/api/cost-observations`.

Vault item `AutomaticAI Cost Pipeline - James Brennan` contains the two
scoped bearer credentials and explicit client/project IDs. Collector secrets
are the declared references under `/james-brennan/costs/prod/` in AWS SSM.
The portal stores only the ingestion token's SHA-256 plus its scope.
`Cloudflare-AutomaticAI-Portal-Deploy/credential` is the scoped deployment
token, also installed as this repository's GitHub `CLOUDFLARE_ACCESS_TOKEN`.
No raw credential was persisted in source, scratch files or Docker login
configuration.

`BLOODBANK_EVENTS` retains its existing seven-day policy. Do not alter it for
this service. Both S3 and the bridge keep original envelopes beyond that
retention; replay preserves event IDs, revision numbers and timestamps.
Portal deduplication therefore remains effective after NATS's two-minute
deduplication window. Preserve the Docker archive volume on upgrades and
include it in normal persistent-volume backups. S3 is an independent replay
source if that host volume is lost.

Unknown providers require operator evidence before becoming billable. A
separate review must establish disjoint allocation before any historical
shared spend is included. Publishing always requires a reviewed immutable
draft; later corrections cannot modify a published statement.
