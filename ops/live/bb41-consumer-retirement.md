# BB-41: retired legacy delegation consumers

Ticket: <https://plane.delo.sh/33god/browse/BB-41/>

**The exact two obsolete durables were retired successfully on 2026-11-17,
between 19:54:49.611511Z and 19:54:49.690010Z. Do not rerun the deletion.**
Both workflows remain inactive/unpublished. A read-only observation at
20:05:10.981076Z, **621.291066 seconds after the immediate after-snapshot**,
passed the landed postcheck against the operator's before-snapshot. It found
exactly the two intended removals, no recreated targets, unchanged unrelated
consumer configurations/creation identities, and active polling replacements.

Bloodbank code `059788b23979c4cdc1bc5a974ea13e19ca8bb4b1` landed in main
`456257995d9bad42afb10eef21946cfe317f3940` before the operation. The corrected
independent safety review passed; its record is retained with the operation.
The older F1-blocked review of `286ed326` was **not** the approval used.
Waybill's diagnostic correction `76956b57412bd8331368862c4c7477e6c27891d2`
landed in 33GOD superproject main `52fa51a`. This followthrough changed only
Bloodbank documentation/evidence; it did not repeat any live mutation, generate
test traffic, replay, purge, activate, restart, schedule, or write Plane state.

Fresh post-retirement **n8n node execution and correlated agent tool records**
were observed, not just receipts. However, the newly created HEYMA-28 triage and
HOLOC-44 Krebs commands were still pending at capture. Do not claim that a
command created after retirement reached agent tools or completed. The actual
post-retirement tools below belong to authorized commands created earlier.
Parent/reviewer decides whether that bounded evidence satisfies AC4; this record
does not turn the pending commands into successes or assert ticket closure.

## Acceptance-criterion evidence index

Paths in this table are relative to `ops/live/evidence/bb41/`.

| AC | Observed outcome | Evidence |
|---|---|---|
| 1 — identify ownership/mode and verify replacement | Both exact durables were pull, filtered as below, zero waiting/unacked, owned by inactive/unpublished legacy workflows. Ticket Triage and Pickup Chip active; Krebs owns dispatch. Prior actual BB-41 triage/implementation tool execution was independently reviewed. | `reviewed.json`, `handler-evidence.json`, `retirement-20261117/before.json`, `retirement-20261117/independent-review.md` |
| 2 — retire only obsolete durables, keep workflows off | Two exact DELETE responses are `success:true`; operation complete. Inventory 25 → 23; both legacy workflows unchanged/inactive/unpublished before, immediately after, and later. | `retirement-20261117/{operation,before,after,later,verification}.json` |
| 3 — no replay, record skipped backlog | Initial exact filtered retained enumeration preserved; operator-before pending was 1,947 / 1,554. Skip obsolete delegation positions, not stream data. Stream limits retention/config/creation and all 23 unrelated consumer configs/creation identities unchanged. | `retained-ranges.json`, `retirement-20261117/{before,operation,later,verification}.json` |
| 4 — no recreation and fresh supported handler execution | Targets absent after 621.291066 s and a scheduled Krebs tick. Fresh triage/skip and Pickup Chip nodes executed; TOLT-147 and HEYMA-19 have correlated actual post-cutoff tool records. **Limit:** their commands predate retirement; the two new commands remain pending, so new-command agent execution is not proven. | `retirement-20261117/{later,later-postcheck,candybar-observation,post-handler-evidence,gateway-followup,verification}.json` |
| 5 — same-stream survey and existing diagnostic | All seven remaining n8n pull durables waiting, zero pending/unacked. Landed Waybill parser on actual monitoring GET reports no n8n idle/residual candidates. Tests cover >24h idle candidate, waiting/bound, zero backlog, short age, and exact retired identity. No continuous 24h history claimed. | `retirement-20261117/{later-audit,waybill-live-parser}.json`, `retirement-20261117/waybill-tests.txt`, Waybill commit `76956b5` |
| 6 — operating notes and evidence | This runbook, immutable operation copies, independent review, source-summary capture script, regression outputs and checksum manifest distinguish fixtures from live observations and pending commands from execution. | `retirement-20261117/README.md`, `retirement-20261117/verification.json`, `retirement-20261117/SHA256SUMS` |

## Diagnosis and backlog disposition

`BLOODBANK_EVENTS` uses **limits** retention with a seven-day maximum age.
Deleting these consumers discarded only their positions, not stored events.
The backlog is obsolete delegation work, not work to re-drive. **Skip it
permanently for these two lanes; never replay, fetch/ack to drain, purge, or
activate them.** Krebs lifecycle v2 reserves implementation dispatch to
`krebs tick`; Todo entry starts triage, not implementation.

| Exact durable | Workflow | Actual mode/filter | Operator-before, 19:54:49.611511Z |
|---|---|---|---|
| `n8n-l0J53w0TsS5PwETk-8b302a2c-5f47-5294-88de-9fbd52b2822b` | `l0J53w0TsS5PwETk`, Ticket Delegation — Groomed in Todo | pull; `bloodbank.evt.agent.invocation.completed` | 1,947 pending, 0 unacked, 0 waiting |
| `n8n-8mmqdMwQYA28ZwUj-d1000000-0000-4000-8000-000000000001` | `8mmqdMwQYA28ZwUj`, Ticket Delegation | pull; `bloodbank.evt.repo.task.updated` | 1,554 pending, 0 unacked, 0 waiting |

Both had null `activeVersionId`. Last delivered was
2026-11-14T11:46:50.628482681Z / 2026-11-14T11:46:39.226867414Z.
Neither config had `deliver_subject`; missing `push_bound` is normal for pull.
The original 1,732 / 1,420 report was an earlier baseline, not live backlog.

The initial read-only **direct stream GET, next_by_subj** enumeration was
bounded to sequence **10,700,394**, at the earlier 19:24:21Z snapshot:

- l0J53: **1,894** retained matching events, first **10,154,805**, last
  **10,700,071**; last delivered 10,154,632.
- 8mmqd: **1,532** retained matching events, first **10,155,132**, last
  **10,698,845**; last delivered 10,154,588.

These exact lists are in `evidence/bb41/retained-ranges.json`. They do not mean
every sequence between the endpoints matched. Natural traffic and expiry
continued before retirement. `operation.json` records the fresh pending counts
and candidate sequence bounds through **10,709,804**; those bounds are **not**
a fresh filtered enumeration. All obsolete backlog then present was deliberately
skipped under the reviewed no-replay disposition.

## Review, operation and no-recreation observation

The independent corrected-revision review is retained as
`evidence/bb41/retirement-20261117/independent-review.md`. It verified the F1 fix:
`capture()` retains both legacy workflow IDs even after their consumers are
absent; real snapshot/postcheck CLI fixtures also reject reactivation. The review
was conditional on fresh guards and a cooperative workflow/consumer change
freeze, not an atomic-delete guarantee or a blanket completion verdict.

The parent performed the already-authorized operation with the landed guarded
runner. Original records remain at the active profile's scratch directory
`bb41-authorized-operation-20261117/`; the committed before/operation/after files
are byte-identical copies. Both exact broker responses succeeded and the
immediate postcheck passed. The operation's nested
`disposition.live_mutation_authorized:false` is the read-only planner's static
output, **not** a failed authorization check: the enclosing real operation record
contains the authorization/review attestations, successful responses and
`complete:true`.

The existing Candybar API returned `krebs-tick`, enabled/non-archived,
`*/10 * * * *`, America/New_York. One bounded foreground wait was used; no clock
or polling loop was created. Scheduled invocation
`06893f74-9269-5d70-abc0-08a1300a866d` ran at 20:00:00.387785Z and finished
20:00:08.154115Z, success, exit 0. The later snapshot at 20:05:10.981076Z
spans more than one full 600-second interval after deletion:

- Both exact retired durables absent; both legacy workflows unchanged/off with
  null active versions; no legacy workflow execution started after cutoff.
- All **23** unrelated consumer configs and creation identities, stream config
  and creation identity, and broker identity unchanged.
- Ticket Triage and Pickup Chip active/polling; all **seven** remaining n8n pull
  consumers had one waiting pull, zero pending and zero unacked.

This is a bounded pair of endpoint observations plus intervening natural handler
execution, **not** uninterrupted 24-hour telemetry or proof against all future
recreation. Do not recreate a retired durable as rollback.

## Actual post-retirement handler execution

`retirement-20261117/post-handler-evidence.json` is derived from read-only n8n
`execution_entity`/`execution_data`, gateway execution metadata, correlated
profile session/message databases and the existing Krebs journal. It retains
node status/timing/branch counts, allowlisted identifiers, timestamps and hashes,
not prompt bodies, tool arguments/results, node code, headers or credentials.

- **742030**, 19:54:55.993Z: Ticket Triage ran Triage Ticket and Triage Started
  for natural HEYMA-28 Todo traffic; `groomTicket`, correlation
  `79e769d6-964b-5f15-8cec-f3205e68b962`, command
  `a97f5d82-a1f0-530b-abdc-56add9433327`. The nodes succeeded, but the gateway
  command remained **pending**; no agent-execution claim for this command.
- **742034**, 19:54:56.781Z: Triage Ticket correctly skipped HEYMA-27 because
  the phase was Done. **742060**, 20:00:04.101Z: it correctly skipped HOLOC-44
  because the phase was In Progress. These are executed guards, not dispatch.
- **742048**, 19:58:06.203Z: Pickup Chip handled the natural TOLT-147 completion
  event `3d3941a8-3e59-5792-836e-efd481ac95e6`; label lookup, issue/activity
  reads, planning, write and check nodes all ran successfully. Exact event-ID
  matching joins gateway command `e1a4ec36-59c3-5b39-b3a8-d195267eaf11` to
  correlation `ce7e8beb-8187-555a-98d8-92e8a5ca3f9b` and session
  `20261117_145235_0337f180`. That session has **12 actual tool-result records
  and nine assistant tool-call records after retirement**, 19:55:03–19:57:54Z;
  the command completed successfully at 19:58:06.180453Z. It was created at
  19:39:35Z, **before** retirement; this is fresh completion/continued execution,
  not a new post-retirement command.
- **742049**, 19:58:06.454Z: Pickup Chip handled HEYMA-19's natural started
  event `df7b00ab-c566-57e5-84ab-2a3556d14814`, ran label/issue reads and
  Needs Write (no write needed). The exact event joins command
  `6b830397-6ff6-575f-a509-66db69b20e59`, thread correlation
  `43c120da-2093-5b39-b20e-8b7e9bdbdc30`, session
  `20261113_142125_9a986331`: **six actual post-retirement tool-result records
  and three assistant tool-call records**, 19:58:22–19:58:37Z. It was created
  19:40:03Z, before retirement, and remained started, not completed.
- The scheduled 20:00 Krebs tick wrote/published HOLOC-44 command
  `32cbef44-72c2-5809-a0e0-0a8f19066a6b`. `gateway-followup.json` records it
  **pending**, not proven agent execution. The continuing BB-41 implementation
  has one post-cutoff tool result (the operation return); that is corroboration
  only, not independent fresh-handler proof.

No synthetic ticket, command or event was produced and nothing was replayed.
The evidence gap is specifically **agent execution of a command created after
retirement**. It does not erase the real post-retirement node runs and correlated
tool execution above, but must remain visible rather than upgrading a receipt.

## Existing Waybill diagnostic resolved and exercised

The owning source is
`33god-platform/waybill/waybill/collectors/nats.py` in the 33GOD superproject.
Landed `76956b5` preserves its on-demand, turn-subject-matched behavior. An n8n
durable with pending backlog, no waiting pull/bound push and delivery older than
24 hours is an **orphan candidate**, never proof of continuous outage. The two
exact historical names get a visible `retired_lane_residual` diagnosis requiring
current ownership verification, not a prefix-based healthy exemption. A waiting
pull, bound push, zero pending, shorter age or missing delivery time does not
produce that n8n idle finding. The NATS snapshot cannot infer workflow activation.

The **landed parser** (`parse_jsz` → `idle_consumers`) was exercised on the
actual monitoring GET saved in `later.json`, not fixture data. Subject inputs
were live n8n filters plus the exact retired filters; no event was published.
`waybill-live-parser.json` records seven n8n consumers, **zero retired residuals
and zero n8n idle candidates**. This is an explicit parser survey, not a claimed
production end-to-end Waybill turn capture. `later-audit.json` separately joins
current workflow state. No additional same-stream n8n candidate was observed;
no continuous 24-hour absence claim is possible from these snapshots. The
conditional existing-check extension is landed; no new monitoring platform or
recurring schedule was installed. Candybar remains the only recurring clock.

## Recheck read-only; do not repeat the live step

From the landed checkout (or the task worktree), use a **new** private scratch
output path. `snapshot` refuses overwrite; it uses HTTP monitoring GET and SQLite
`mode=ro`/`query_only`. These commands never fetch/ack/create/delete a consumer:

```bash
E=/home/delorenj/.hermes/profiles/bloodbank-pm/cache/scratch/bb41-observe-next
mkdir -m 700 "$E"
python3 ops/live/bb41_consumer_retirement.py snapshot --out "$E/later.json"
python3 ops/live/bb41_consumer_retirement.py postcheck \
  ops/live/evidence/bb41/retirement-20261117/before.json "$E/later.json"
python3 ops/live/bb41_consumer_retirement.py audit "$E/later.json"
```

A mismatch requires attribution, not modifying unrelated consumers to force a
pass. If either target returns, investigate current ownership read-only; never
activate, replay, automatically delete again or recreate a durable as rollback.
The retained runner has no atomic two-object delete or automatic rollback; its
historical operation record is not authorization for another live action.

## Verification and evidence retention

- Landed Bloodbank isolated suite: **23 tests, OK**, exit 0; includes real private
  HTTP/SQLite capture/postcheck boundary and reactivation refusal, guard failures,
  exact DELETE subjects, fail-stop and injected runner. These tests use fixtures,
  not production deletion.
- Landed Waybill suite: **299 tests, OK**, exit 0; includes corrected n8n idle
  boundaries and `collect` integration with mocked GETs. Live parser input is
  separately evidenced as above.
- Real `postcheck operator-before later`: **exit 0**, `ok:true`,
  `only_intended_consumers_removed:true`. Its intentional
  `handler_execution_proven:false` means the postcheck validates state, not
  handler execution; the separate node/session sources supply the latter.
- `retirement-20261117/verification.json` indexes actual commands, timestamps,
  assertions, source revisions, test counts and limits. `README.md` explains each
  file; `SHA256SUMS` covers the package. Verify with `sha256sum -c SHA256SUMS`
  inside that directory; the original six-file manifest remains unchanged.

Older `evidence/bb41/{before,reviewed,readonly-check,retained-ranges,handler-evidence}.json`
are **pre-retirement** evidence. `evidence/bb41/tests.txt` is the historical
22-test run on the blocked revision, not the corrected 23-test result. The copied
independent review's future-tense remaining work is a historical safety decision;
this later operational record supplies subsequent facts without rewriting it.
Parent retains responsibility for external-handle verification, review, main
integration of this evidence branch, and any ticket verdict. No Plane state or
labels were changed by this followthrough.
