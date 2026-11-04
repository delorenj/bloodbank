# Dev Journal retry-mail repair — review and release handoff

Status: feature branch only; **not installed, deployed, activated, or independently reviewed**.

## Causal evidence (read-only, 2026-11-03 EST)

The parent verified Gmail thread `1a1a2ab4a3d2dcad`: 23 repeated “Updated: Dev Journal — 2026-10-24 (processing pending)” messages at roughly 15-minute intervals, 18:28–23:58 UTC. This worker independently decoded actual n8n execution records (flatted reference JSON), not simulated provider responses:

| Execution | Started UTC | Report key | Partial errors | Mail hash | Actual Resend receipt |
| --- | --- | --- | ---: | --- | --- |
| 583338 | 2026-11-03 23:40:00.012 | 2026-10-24:c3fe75fa68ad44fba61754384e9d8320 | 50 | bff04773 | 01a1a3cb-c2d3-7039-857e-58a25efcfe96 |
| 583558 | 2026-11-03 23:55:00.018 | same immutable generation | 36 | c9f370b2 | 01a1a3d9-2fdc-740f-a043-208a4c55135c |

Both have all seven processor steps through “Store Resend receipt”. Errors include `The service is receiving too many requests from you` on Plane requests. Different findings/order/errors change rendered markdown. `process-report.js` hashes that markdown, then `prepare-email.js` compares only the previous hash: a changed partial body creates a new Resend idempotency key. A five-minute native trigger + ~three-minute processing + ten-minute retry delay produces roughly fifteen-minute mail spacing.

Actual state is `~/.local/state/dev-journal/state.sqlite`, selected by installed `JournalStore.js` (or `DEV_JOURNAL_DB_PATH` override). The n8n `data_table_user_hkbQ9h8HNwZJee9Q` table has **zero** rows; it is not the current journal store. The actual report is partial, attempt **311**, last receipt at `2026-11-03T23:58:02.298Z`, next retry `2026-11-04T00:08:02.028Z`; content SHA-256 is `7029aa34259045adf31e7cda992d3768924001620ba65a45d8bb17fca005b38a`.

The editor workflow's claim node says `rollup` (updated 17:07:46.818 UTC), but execution snapshots 583338, 583414, 583488 and 583558 all say **selectReport**. The intermediate two-node executions simply find no due report. Do not blame rollup for the captured emails, nor blindly activate the edited draft. Source already correctly used selectReport; the regression assertion now locks that mapping. Deployment must compare draft and published versions, retain selectReport, and read back the published version after activation.

Candybar nightly `01a17951-0470-7f83-8bbd-dfa5c7ce30f2` remains daily `0 6 * * *` America/New_York. It is not the retry sender and must remain intact. The parent paused processor `130a105d-c52e-4437-a53f-7d82c918cfd9`; fresh read-only SQLite evidence confirms inactive. This worker did not change it.

## Candidate behavior

- Logical identity is immutable report key + source content digest + `partial`/`complete`, not model prose/errors. One initial partial maximum, then one optional completion; a genuine correction is a newly ingested generation.
- Successful legacy receipts consume the partial slot. Additive store migration infers a legacy complete receipt from complete status before processing can overwrite it. No receipt or payload is deleted.
- `pending_email` freezes exact payload and provider key before transport. Failed/ambiguous provider responses retain it across attempts/restarts. Successful receipt clears it and records `email_phase`.
- Deadline and processor share this outbox; no new mail loop or timer is added. No exactly-once guarantee is claimed across arbitrary concurrent writers or provider idempotency retention expiry; single-clock cutover must prevent overlapping old/new workflows. Never clear pending mail to force delivery without reconciling provider acceptance.
- Source exports replace only processor/deadline/incidents/rollups native schedule triggers with POST webhooks using last-node response. All exports and four Candybar manifests remain disabled. Event-driven ingress and manual backfill behavior are unchanged.

## Verification

- `node build.mjs && node --test test/*.test.mjs` from this directory: **21 passed, 0 failed, exit 0**.
- `node_modules/.bin/tsx --test test/dev-journal.test.cjs` from `../../n8n-nodes-bloodbank`: **3 passed, 0 failed, exit 0**. Includes additive migration and reopen preservation.
- `npm test` in custom node package: build/TypeScript succeeded; **239 passed / 240, 1 failed, exit 1**. Existing `trigger-options.test.cjs:228` generated sample for `bloodbank.agent.invocation.start` has `deadline_seconds` zero but schema requires >0. Unchanged main independently reproduces **17 passed / 18, exit 1** for that test file. Not repaired outside scope.
- All four manifests pass the real `candybar.registry.manifest.load_manifest` parser, enabled false, n8n runners, expected schedule/timezone. No registration performed.
- Exact captured execution payload replay on old prepare code produces **1 duplicate**; candidate produces **0**. Candidate built package replay against a SQLite backup of the real store returns zero for both executions, **71 reports before/after**, old email_id retained, migrated phase partial. No provider/network used. Private large payloads remain only in profile scratch, not Git.

## Independent review gates

Review implementer != reviewer. Check lifecycle suppression, completion after failed send, deadline sharing, old-state inference, immutable corrections, provider retry bytes, and concurrency/retention caveat. No live activation before review and explicit operator release. Full-suite known failure must remain visible rather than claiming green. Plane throttling remains an operational problem: suppression fixes mail noise, not API capacity.

## Release commands — future authorized operator only

Do **not** execute these during review. First land the reviewed Bloodbank feature commit through normal independent review; do not advance root pins from an unreviewed feature.

1. Snapshot `~/.local/state/dev-journal/state.sqlite` using SQLite's online backup API into operator-owned backup storage; preserve all n8n workflow/version/export histories. Save API GET readbacks of the four exact workflow IDs below. Never edit n8n SQLite or delete journal rows.
2. In the reviewed canonical Bloodbank checkout:

   ```sh
   cd /home/delorenj/code/33GOD/bloodbank/integrations/n8n-workflows/dev-journal
   node build.mjs
   node --test test/*.test.mjs
   cd ../../n8n-nodes-bloodbank
   npm ci --ignore-scripts --no-audit --no-fund
   npm run build
   node_modules/.bin/tsx --test test/dev-journal.test.cjs
   npm run deploy -- --check
   # After separate deployment/restart authorization only (this restarts n8n):
   npm run deploy
   ```

   `--check` is read-only for installed runtime (exit 3 means expected drift). Deploy installs built dist by content hash, verifies it and restarts n8n. Package version alone does not prove installation. Confirm installed `programs.generated.js` contains `dev-journal-v2:` and installed JournalStore has additive migration, before any activation.

3. Retire native clocks through supported API, one bounded operator action (no new loop/timer). Load API key only into process memory using `op read 'op://DeLoSecrets/n8n/Saved on localhost/api key'`. API base `http://127.0.0.1:5678/api/v1`; use `X-N8N-API-KEY`. For each exact ID: `POST /workflows/{id}/deactivate` body `{}`, then GET and require `active:false`. Do not touch ingress `1e1599de-4198-4090-a884-e307d035a6c2`.

   | Source file / manifest lane | Exact live ID | Candybar wall schedule |
   | --- | --- | --- |
   | processor | 130a105d-c52e-4437-a53f-7d82c918cfd9 | */5 * * * * |
   | deadline | b560415e-7b5a-46cf-a8fe-9c5d6e5fe1e2 | 0 7 * * * |
   | incidents | ca9dcb49-ba16-402b-a49d-aa3748a53019 | */5 * * * * |
   | rollups | f5dee132-d9fa-425b-ab00-046112f49a0b | 30 8 * * * |

   For each, compare fresh live GET nodes/connections/settings/credentials with reviewed source. Preserve unrelated live configuration. Publish only reviewed changes via supported `PUT /workflows/{id}` with `{name,nodes,connections,settings}` (omit read-only id/active/meta fields); require GET readback equals reviewed graph and inactive. Source deterministic IDs match the IDs above. Do not blindly run `/home/delorenj/code/infra/scripts/deploy-dev-journal-workflows.sh activate`: it imports seven canonical exports and activates all five, including ingress. Its canonical path must contain the reviewed source first; this release only needs four targeted workflows, not backfills or ingress.

4. Register each committed manifest **disabled** from Candybar repo; no `candybar run` for these side-effecting jobs:

   ```sh
   cd /home/delorenj/code/33GOD/candybar
   uv run candybar register /home/delorenj/code/33GOD/bloodbank/integrations/n8n-workflows/dev-journal/candybar/processor
   uv run candybar register /home/delorenj/code/33GOD/bloodbank/integrations/n8n-workflows/dev-journal/candybar/deadline
   uv run candybar register /home/delorenj/code/33GOD/bloodbank/integrations/n8n-workflows/dev-journal/candybar/incidents
   uv run candybar register /home/delorenj/code/33GOD/bloodbank/integrations/n8n-workflows/dev-journal/candybar/rollups
   uv run candybar show dev-journal-processor -n 5
   ```

   Register is create-only: a duplicate 409 is not success; inspect existing row/runner and deliberately update through supported APIs, never delete history. Record disabled IDs and exact old native identities in Candybar migration ledger; n8n is not a supported native kind of automated `migrate cutover`, so do not invent a ledger native kind or run that command against these clocks. Record this bounded API cutover and receipts in a reviewed migration entry/document.

5. With native triggers absent in all four fresh published graphs, activate exact webhook workflows via `POST /workflows/{id}/activate`; GET verify active and published claim operation selectReport. This is webhook activation, not schedule activation. It needs independent approval. Then `uv run candybar resume dev-journal-processor` (and the other three names) only after readback, keeping daily-six producer unchanged. Observe the first **scheduled** invocation and actual processing/receipt; never send a test mail just to prove dispatch.
6. `uv run candybar history dev-journal-processor -n 5`; audit `uv run candybar doctor --repo . --live --json --n8n-export /absolute/path/to/fresh-export.json`. Empty queue/suppression is valid no mail; verify no repeated partial receipt, successful completion when dependencies recover, and daily-six producer intact. Parent's existing doctor exit 2 / unrelated timers and unknown Hermes crank owner remain unresolved and must not be concealed or migrated here.

If release fails, pause the new Candybar registrations and keep processor inactive; restore reviewed source with webhook clocks only. **Do not roll back to the known spammer or re-enable native schedules.** Preserve pending outbox bytes and reconcile uncertain Resend acceptance before retry outside provider retention.
