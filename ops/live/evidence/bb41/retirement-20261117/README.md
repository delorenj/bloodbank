# BB-41 retirement evidence — 2026-11-17

Operating notes and AC-by-AC index: [../../../bb41-consumer-retirement.md](../../../bb41-consumer-retirement.md).
This package records an already-completed operation and read-only followthrough.
It is not an instruction or authorization to run the deletion again.

## Files

| File | Source and meaning |
|---|---|
| `before.json` | Byte-identical parent operator snapshot, 19:54:49.611511Z: both targets present; workflow summaries/hashes and complete broker configuration, no payloads. |
| `operation.json` | Byte-identical parent operation journal: exact two successful DELETE responses, review/authorization references, backlog disposition, `complete:true`, immediate postcheck. The nested planner `live_mutation_authorized:false` is static plan metadata, not the outer execution result. |
| `after.json` | Byte-identical immediate after snapshot, 19:54:49.690010Z. |
| `later.json` | Landed `bb41_consumer_retirement.py snapshot` output from actual monitoring GET and read-only n8n SQLite, 20:05:10.981076Z; 621.291066 seconds after immediate after. |
| `later-postcheck.json` | Real landed CLI result comparing operator-before to later, exit 0. The postcheck deliberately does not prove handlers; use separate handler evidence. |
| `later-audit.json` | Real landed CLI audit: seven remaining n8n pull consumers, all polling with zero pending/unacked. |
| `candybar-observation.json` | Actual manifest/history GETs: existing enabled ten-minute `krebs-tick`; scheduled 20:00 invocation succeeded. No schedule created or manually fired. |
| `independent-review.md` | Historical independent PASS on corrected Bloodbank `059788b` and Waybill `76956b5`, with exact conditions and future postconditions as they stood before the operation. This record was copied, not rewritten. |
| `post-handler-evidence.json` | Read-only node run-data metadata, safe identifiers and hashes; exact lifecycle event-ID joins to gateway commands and profile session tools. Five post-cutoff n8n runs; no legacy executions started after cutoff. Actual tools are counted only after retirement. No prompt/tool/node payloads retained. |
| `gateway-followup.json` | Separate read-only command metadata, including new scheduled HOLOC-44 command; pending stays pending. |
| `waybill-live-parser.json` | Landed Waybill parser on `later.json` actual monitoring GET, with source digest and all n8n filter inputs; seven current n8n consumers, no retired residual or idle candidate. Not a production end-to-end turn capture. |
| `capture-source.py` | Executed one-shot evidence extractor source. SQLite sources are mode=ro/query_only; HTTP calls are monitoring/manifest/history GETs. It writes only new local evidence files, and never publishes, polls, creates/deletes/fetches/acks a consumer, activates, restarts or writes Plane. Takes an evidence directory with `after.json` and `later.json`; refuses output overwrite. |
| `bloodbank-tests.txt` | Real landed 23-test isolated unittest output, exit 0. Private HTTP/SQLite fixtures, not another production operation. |
| `waybill-tests.txt` | Real landed 299-test unittest output, exit 0. Includes mocked collector GET integration and health boundary cases; not live delivery proof. |
| `verification.json` | Programmatically checked operation/snapshot assertions, source revisions, elapsed interval, test commands, handler counts and limits. |
| `SHA256SUMS` | SHA-256 manifest for every other file in this directory. |

## Key outcomes and limits

- The two exact obsolete durables were absent immediately after and after more
  than one existing 600-second interval. Both legacy workflows stayed unchanged,
  inactive and unpublished. All 23 unrelated consumer configurations/creation
  identities, stream config/creation and broker identity match operator-before.
- Triage nodes ran naturally for HEYMA-28 and guarded Done/In Progress traffic.
  Pickup Chip ran naturally for TOLT-147 completion and HEYMA-19 start traffic.
  Exact event/correlation/command/session joins show 12 and six actual
  post-retirement tool results respectively, not only receipts. Those commands
  were **created before retirement**. The BB-41 operation-return tool result is
  corroboration only, not an independent test of new lifecycle execution.
- Newly created HEYMA-28 triage and HOLOC-44 Krebs commands were **pending** at
  followup; agent tools for a newly created post-retirement command are not
  proven. Preserve that precise gap if the acceptance interpretation requires it.
- This is a bounded operational observation, not continuous 24-hour availability
  or absence telemetry. The Waybill parser warns about candidates, not a proven
  continuous outage or current workflow activation.
- No synthetic traffic, replay, purge, activation, restart, schedule change or
  Plane write was performed by this followthrough. Natural Pickup Chip writes
  in n8n's existing workflow are observed, not agent-issued test mutations.

## Reproduce safe verification

From this directory:

```bash
sha256sum -c SHA256SUMS
python3 ../../../bb41_consumer_retirement.py postcheck before.json later.json
python3 ../../../bb41_consumer_retirement.py audit later.json
```

The original pre-retirement evidence and original `../SHA256SUMS` were left
unchanged. For another live read-only snapshot, follow the runbook and choose a
new private scratch output; do not overwrite this historical package.
