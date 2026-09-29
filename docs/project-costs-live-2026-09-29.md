# Project cost pipeline: deployed proof and operations

The independent collector, archived Bloodbank ingress and portal cost ledger
are deployed for James Brennan. The current-month backfill completed and the
six-hour schedule is enabled. This path never calls Relay, Voice, GorillaDesk
or the phone release controller. No draft, statement, invoice, notification or
synthetic production client was created during deployment.

## Deployed identities

| Component | Verified artifact or resource |
| --- | --- |
| Portal source | `AutomaticAI-io/client-portal` main `a74a46590f440769877fd273bb3be8b22b760942` |
| Portal Worker | `automatic-ai`, version `79e7004e-a8ce-4214-beed-e196975c0f8a` |
| Portal migrations | Additive `0004_project_costs.sql` and `0005_cost_statement_amendments.sql` |
| Bloodbank source | `delorenj/bloodbank` implementation `b501004b3e3ec4336531da783c8942d76a1d271e` |
| Bridge running image | `sha256:ca3080613c899bc85b9d59e4a580f38fc5ebb4d3dfb14eaa71138aec96723a7e` |
| Bridge archive | Docker volume `bloodbank-cost-bridge_cost-archive` |
| Collector source | `client-portal` implementation `611db23ff2476e5576fa66d07ba52ab7f647604c` |
| Collector image | `067200612963.dkr.ecr.us-east-1.amazonaws.com/automaticai/project-cost-collector@sha256:5e9a4096a9e0aa16c16c39665cb1ec3c657dde0d0b215505792f4f5e1ebaf21c` |
| Collector task definition | `arn:aws:ecs:us-east-1:067200612963:task-definition/automaticai-project-cost-collector:6` |
| Successful September backfill | ECS cluster `james-brennan-relay`, task `9ff60ae11d97489489a31806a35f87c4`, exit 0 |
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

The read-only verification on September 29 found 228 original observations in
S3, D1 and the bridge archive, with identical event IDs across S3 and D1,
zero pending publication, zero pending projection, zero delivery errors,
an empty S3 outbox and zero statements. Seven monetary source documents were
read back from S3 and their SHA-256 digests matched the ledger evidence.
The upgraded collector recovered the existing archive before collecting new
snapshots. Its six-hour schedule reconciles the current month and two recently
closed months. Observation counts increase as that schedule runs.

| Provider | Latest observed USD at verification | Treatment |
| --- | ---: | --- |
| AWS | 127.582711 | Vendor estimate, shared account, unallocated |
| Deepgram | 229.622780 | Actual key-scoped expense; September includes historical staging |
| Twilio | 21.165160 | Actual subaccount expense; September includes historical staging |
| OpenRouter | 6.130374 | Actual monthly key usage; LLM expense excluded from rebilling |
| Dedicated staging Deepgram, Twilio and OpenRouter | 0.000000 each | Observed actual zeros in isolated staging scopes; excluded from rebilling |

These are dated observations, not frozen future totals. All four remain
private and excluded from billable September totals. An actual AWS observation
supersedes an estimate for the same expense scope; estimates never become
actuals by relabeling. An unavailable refresh retains the last sourced money as
stale reference evidence, excluded from another draft. Cloudflare, Cartesia, Resend, Clerk, Langfuse,
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

The production deploy gate passed 522 portal tests across 40 files, type
checking and strict build. The cost gate passed 18 collector/storage tests,
seven bridge tests and 27 focused portal ledger/flow/UI tests. The isolated rehearsal uses real HTTP,
JetStream, the durable subscriber and the actual D1 adapter. Two fixture
clients generate 30 observations; stream deletion followed by archive replay
restores all 30 with no duplicate rows. Wrong credentials and tenant bindings
are rejected. The rehearsal also checks the portal's vendored schema against
Bloodbank's canonical contract.

Bridge tests cover persistence before PubAck, restart/replay, token/scope refusal,
signed credits, timestamp offsets and acknowledgement only after the portal
confirms that same event's commit. A mismatched HTTP receipt remains pending
and requests redelivery. The consumer fetches one projection at a time so ACK
timers do not expire behind earlier HTTP work. Portal tests use actual session/membership/operator gates
and SQLite transactions for authorization, manual/API revision interleaving,
source authorship, negative credits, current-month provisional totals and
immutable published amendment chains. The additive migration preserves an
existing published statement while admitting a reviewed full replacement
linked to it; replacements are never presented as additional charges. No
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
draft; later corrections create a separately reviewed immutable replacement
without modifying the previous publication.
