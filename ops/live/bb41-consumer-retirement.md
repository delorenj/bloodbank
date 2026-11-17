# BB-41: exact consumer retirement, review before live step

Ticket: <https://plane.delo.sh/33god/browse/BB-41/>

**This change has not retired anything.** The first independent review of
`286ed32606b9b69a758d1c1e1efcd69c56de1dca` blocked live execution:
`capture()` dropped both target workflows after consumer deletion, causing
`postcheck()` to fail after two irreversible DELETEs. The corrected branch
retains the exact workflow IDs regardless of consumer inventory and tests the
actual HTTP/SQLite snapshot-to-postcheck path, including a reactivation refusal.
**Do not use the blocked review record as approval.** Independently review the
corrected revision before running the live step. The user's existing BB-41
instruction already authorizes the exact targeted retirement after a passing
independent safety gate; `--authorization-ref` attests that instruction, not a
new permission request. The implementation worker performed only
read-only live diagnostics. No workflow, consumer, stream, Plane record, service,
scheduler or other repository was changed. Branch publication is not live-step
approval or ticket completion.

## Diagnosis and backlog disposition

`BLOODBANK_EVENTS` uses **limits** retention and a seven-day maximum age.
Deleting these consumers discards only their positions, not stored events. The
backlog is obsolete delegation work, not work to re-drive. **Skip it permanently
for these two lanes; never replay, fetch/ack to drain, purge, or activate them.**
Krebs lifecycle v2 reserves implementation dispatch to `krebs tick`. Todo entry
starts triage, not implementation. See `krebs/docs/lifecycle.md` and
`krebs/spec/lifecycle.v2.yaml` in the existing Krebs checkout.

| Exact durable | Workflow | Actual mode/filter | Before at 19:24:21Z | Reviewed at 19:31:41Z |
|---|---|---|---|---|
| `n8n-l0J53w0TsS5PwETk-8b302a2c-5f47-5294-88de-9fbd52b2822b` | `l0J53w0TsS5PwETk`, Ticket Delegation — Groomed in Todo | pull; `bloodbank.evt.agent.invocation.completed` | 1,894 pending, 0 unacked, 0 waiting | 1,897 pending, 0 unacked, 0 waiting |
| `n8n-8mmqdMwQYA28ZwUj-d1000000-0000-4000-8000-000000000001` | `8mmqdMwQYA28ZwUj`, Ticket Delegation | pull; `bloodbank.evt.repo.task.updated` | 1,532 pending, 0 unacked, 0 waiting | 1,536 pending, 0 unacked, 0 waiting |

Both workflows were inactive with null `activeVersionId` at both observations.
Last delivered: 2026-11-14T11:46:50.628482681Z and
2026-11-14T11:46:39.226867414Z respectively. Neither config has a
`deliver_subject`, so neither is a push consumer. Missing `push_bound` is normal
for pull. Original report counts (1,732/1,420) were an earlier baseline, not the
current backlog.

Read-only **direct stream GET, next_by_subj**, bounded to the initial snapshot's
last sequence 10,700,394, enumerated exactly:

- l0J53: **1,894** retained matching events, first sequence **10,154,805**, last
  **10,700,071**. Last delivered was 10,154,632.
- 8mmqd: **1,532** retained matching events, first sequence **10,155,132**, last
  **10,698,845**. Last delivered was 10,154,588.

The exact sequence lists are in `evidence/bb41/retained-ranges.json`. These are
as-of counts/ranges, **not** a claim that every sequence between endpoints
matches. New matching events continue to accumulate and natural stream expiry
continues. The live runner saves a new before-snapshot and its exact pending
counts immediately before deletion; its sequence bounds are explicitly labelled
candidate bounds, not enumerated filtered ranges. All backlog then present is
intentionally skipped under the same no-replay disposition.

## Replacement: actual execution, not just receipt

Read-only evidence is in `evidence/bb41/handler-evidence.json`:

- Ticket Triage `6wAGA5pdrmHLyhs2` is active, listening to repo.task.created and
  repo.task.updated through a pull durable, with a `Todo` phase guard and no
  `delegateTicket` operation. Its default Fleet operation is `groomTicket`.
- BB-41 triage ran as n8n execution **741382**, command
  `849e2e44-6e65-503f-ad27-59a22e7868df`, at 18:21:28Z. Gateway execution
  completed successfully at 18:43:54Z. The correlated bloodbank-pm session has
  **54 actual tool-result records and 29 assistant tool-call records** inside
  that triage window. The records include names, timestamps and content hashes,
  not secret-bearing payloads. This is agent execution, not a publish receipt.
- Krebs journal records BB-41 dispatch.write and dispatch.publish at
  19:19:45Z, dispatch `876e72e5-eb8f-570c-9858-15e1593e2317`, command
  `fb758fac-bdb9-5b56-a2b8-1c83e23fcfd4`. The same correlated agent session has
  **five actual tool-result records and three assistant tool-call records after
  this command started**. It is still running, not claimed completed.
- n8n execution **741762** actually ran Triage Ticket for that In Progress
  transition and took its skip branch, not implementation dispatch.
- Active Pickup Chip `wWXgCZiiIBWaRRzE`, execution **741761**, actually ran
  label lookup, live issue read and Needs Write (correct no-op: no write needed).
- Additional fresh triage execution **741626** at 19:00:27Z for HOLOC-41 ran
  Triage Ticket and Triage Started; its command completed successfully at
  19:07:47Z. This corroborates the live lane, but completion alone is not used
  as the BB-41 agent-handler proof above.

The before/reviewed snapshots contain full consumer configurations and activation
state plus non-payload node summaries. The reviewed snapshot additionally hashes
complete workflow nodes/connections/settings without storing their code, headers
or credentials. A second read-only snapshot passed `validate-fresh` with both
consumers still present, no legacy progress, and unrelated config unchanged.

## Same-stream n8n audit and existing health seam

Nine n8n consumers were found. All are pull. The seven outside this repair were
polling (`num_waiting=1`), with zero pending and zero unacked:

- Dev Journal ingress: `n8n-1e1599de-4198-4090-a884-e307d035a6c2-75e4d046-f5dd-48f0-ad15-3739ab8756fb`
- Ticket Triage: `n8n-6wAGA5pdrmHLyhs2-f830070b-2b40-4edd-9d27-687e20fc304d`
- Wax transcripts: `n8n-OTrzey7C2VKe8dr4-2c529827-04d6-46d8-ba1e-71e4c944a839`
- Transcription Notifications, complete: `n8n-XHWRrrYCzu4ifdS7-7b344c02-578e-4f44-8665-d5b3f6c44d82`
- Transcription Notifications, new file: `n8n-XHWRrrYCzu4ifdS7-f3bf0372-0cb4-447f-b0ba-bac7a2f11a6d`
- Genesis ntfy: `n8n-sr604yqfOwRgdmiZ-6c1f3a52-8a0b-4d3e-9c1e-0d6f7b9a1001`
- Pickup Chip: `n8n-wWXgCZiiIBWaRRzE-1a4f5ee5-dd90-5b25-80bb-dc48568f35f0`

No additional same-stream n8n orphan candidate was observed. This is a point-in-
time audit, **not** continuous 24-hour binding history. The two targets have
multi-day delivery inactivity and no waiting pull at each observation; JetStream
pull consumer INFO does not expose a last-disconnected timestamp.

No standing consumer health check covering this seam was found **in Bloodbank**.
The existing diagnostic is in the separate repository
`/home/delorenj/code/33GOD/33god-platform/waybill/waybill/collectors/nats.py`,
`idle_consumers()` (lines 154–168 when inspected). It checks turn-matching
subjects, uses a one-hour idle threshold, and can report a pull durable stale
from last delivery despite a waiting pull. It lacks workflow retirement state
and a 24-hour outage observation distinction. **It has not been edited:** this
worker is authorized to write only Bloodbank. AC5's conditional existing-check
extension therefore remains open for the parent to scope/authorize in its owning
repository; this one-shot audit is not represented as that extension.

The local `audit` helper distinguishes an inactive exact retirement from an
active-lane candidate. A healthy bound push or waiting pull, zero pending, or a
short outage does not receive an orphan finding. One old delivery timestamp is
only a candidate. `audit --previous` can flag two unhealthy endpoint observations
more than 24 hours apart with unchanged consumer identity/config/delivery/ack
positions and pending backlog. This is stronger evidence, not proof of
uninterrupted disconnect between samples. **No recurring check was installed or
clock added. Candybar remains the sole scheduler.**

## Read-only commands (safe before review)

Run from the task worktree or the landed Bloodbank checkout. Set `E` to a new
private directory under the active profile's scratch; never system `/tmp`.

```bash
E=/home/delorenj/.hermes/profiles/bloodbank-pm/cache/scratch/bb41-review-next
mkdir -m 700 "$E"
python3 ops/live/bb41_consumer_retirement.py snapshot --out "$E/before.json"
python3 ops/live/bb41_consumer_retirement.py plan "$E/before.json"
python3 ops/live/bb41_consumer_retirement.py audit "$E/before.json"
python3 -m unittest discover -s ops/live -p 'test_bb41_consumer_retirement.py' -v
```

`snapshot` uses HTTP monitoring GET and SQLite `mode=ro`/`query_only`. It refuses
an existing output file. `plan`, `audit`, `validate-fresh` and `postcheck` only
read saved JSON. None calls consumer fetch, ack, create or delete APIs. They are
stdlib-only. `bb41_delete_consumers.py --help` is safe; **do not invoke its live
runner during review**.

## Independent review gate

Review exact code revision, this procedure, full snapshots and handler evidence.
The reviewer must verify:

1. Exactly the two listed durable/workflow/node identities; pull mode, filters,
   inactive/unpublished legacy workflows, zero unacked/waiting, no legacy progress.
2. Supported Ticket Triage/Pickup Chip polling, current Krebs authority and actual
   correlated agent/tool execution, not solely invocation start or publish receipt.
3. Backlog disposition accepted: deliberately skip all legacy backlog, preserve
   stream retention/configuration and unrelated consumer definitions.
4. Script permits only two hardcoded DELETE subjects; fresh identity/config/
   workflow checks precede them; no wildcard, fetch/ack/replay, purge or activation.
5. Tests pass and remaining Waybill ownership work is explicitly unresolved.

Record a passing independent review of the **corrected exact revision** and
cite the existing user BB-41 retirement authorization for the live step. The
script's reference fields are operator attestations; they do not independently
validate a review service. Do not fabricate either reference to pass its guards.
The earlier blocked F1 review is not a passing review and cannot be used here.
Parent coordinates landing/review and any later operator execution; implementer
must not approve or merge itself.

## Live step — ONLY after the gate above

Use the reviewed snapshot `evidence/bb41/reviewed.json` and handler evidence (or
newly reviewed replacements if anything drifted). Existing environment already
has `nats-py`; check `python3 -c 'import nats'`. Do not install/restart services.
Do not put credentials in argv, files or output. This runner intentionally fixes
the NATS client and monitoring pair to local ports 4222/8222 and verifies server
identity; it does not change their security configuration.

The following is a **future operator command**, NOT executed by this worker.
Use the passing corrected-revision independent review record for `--review-ref`;
use the existing user BB-41 instruction as `--authorization-ref` (a truthful
reference to the authorization already given, not invented new approval):

```bash
python3 ops/live/bb41_delete_consumers.py \
  --reviewed ops/live/evidence/bb41/reviewed.json \
  --reviewed-sha256 b214e2e9d14348b6ce5b372bdfdb812b79ef1d5ec272b102b51e66d1b196d06a \
  --handler-evidence ops/live/evidence/bb41/handler-evidence.json \
  --handler-sha256 ba651b5cf941880b0d09cd5b6dcf38f2ad7a44d50ed8e79d39dff4522202a3f9 \
  --review-ref '<independent-review-record>' \
  --authorization-ref '<live-execution-authorization-record>' \
  --execute DELETE-ONLY-BB-41-RETIRED-DURABLES-NO-REPLAY \
  --evidence-dir /home/delorenj/.hermes/profiles/bloodbank-pm/cache/scratch/bb41-authorized-operation
```

It captures a fresh before-snapshot and blocks on drift (including unrelated
consumer identities/configs or workflow definition hash). New backlog counts
are allowed; legacy delivery/ack progress is not. Broker/capture clocks must
agree within 30 seconds. It then re-reads INFO for each target, persists the new
before-snapshot and operation record, and sends only:

```text
$JS.API.CONSUMER.DELETE.BLOODBANK_EVENTS.n8n-l0J53w0TsS5PwETk-8b302a2c-5f47-5294-88de-9fbd52b2822b
$JS.API.CONSUMER.DELETE.BLOODBANK_EVENTS.n8n-8mmqdMwQYA28ZwUj-d1000000-0000-4000-8000-000000000001
```

It requires `success:true` for each, saves responses and an after-snapshot, and
checks exactly those consumers disappeared while every unrelated consumer's
config/creation identity and the stream config/identity stayed unchanged. It
also checks both legacy workflows stayed unchanged/off and replacement pulls
are polling. No stream purge or publish exists in the script.

**Stop on any mismatch, timeout, I/O failure, partial deletion or postcheck
failure.** Inspect `operation.json` and re-snapshot read-only. Never retry blindly,
recreate a removed durable, restore a legacy workflow, drain or replay. There is
no automatic rollback: recreating a durable could dispatch historical work.
Consumer deletes are not an atomic two-object transaction; evidence may show a
partial operation, and the next safe action requires renewed review.

There is a narrow check/delete race: NATS offers no conditional delete by config
version. A cooperative operator change freeze on these workflows/consumers is a
precondition. If another operator can activate/change them concurrently, **do
not execute**. Stop and coordinate rather than pretending the guard is atomic.

## Postconditions still required after authorized execution

1. Retain operator before/after/operation records and verify only intended
   removals; `postcheck` explicitly does **not** claim handler execution.
2. Observe normal fresh authorized lifecycle traffic without generating a ticket
   or replaying a command just to test. Retain actual n8n node run-data and
   correlated agent/tool execution for triage/Krebs; no dispatch receipt alone.
3. After normal traffic and a full existing Candybar `krebs-tick` interval,
   capture another snapshot and run:
   `python3 ops/live/bb41_consumer_retirement.py postcheck <operator-before.json> <later.json>`.
   Both targets must remain absent and legacy workflows inactive. No new polling
   loop/timer is needed; use the existing clock/normal observation window.
4. If unrelated configuration changed concurrently, stop and attribute it; do
   not “repair” unrelated consumers to force a passing comparison.
5. Parent must resolve the existing Waybill health seam and land through the
   authorized main integration flow before ticket handback. This branch's tests
   and pre-retirement evidence are not full BB-41 completion.

## Evidence files and verification limits

All paths below are relative to `ops/live/`:

- `evidence/bb41/before.json`: full initial broker/workflow snapshot.
- `evidence/bb41/reviewed.json`: full reviewed candidate snapshot with workflow
  definition hashes; this is the live runner's comparison baseline.
- `evidence/bb41/readonly-check.json`: later read-only comparison; targets still
  present, not an after-retirement snapshot.
- `evidence/bb41/retained-ranges.json`: enumerated retained matching sequences,
  no event payloads; initial snapshot hash embedded.
- `evidence/bb41/handler-evidence.json`: sanitized n8n run-data summaries, Krebs
  journal subset, gateway terminal metadata and actual session tool-record hashes.
- `evidence/bb41/tests.txt`: **22 passing isolated tests on the original
  blocked revision**, no production writes. The corrected revision's 23-test
  run must be checked separately; this file is historical evidence, not new proof.
- `evidence/bb41/SHA256SUMS`: content digests; verify with `sha256sum -c` from
  the evidence directory.

Tests exercise health cases, guard refusals, exact deletion subjects, fail-stop
on first deletion error, the complete live-runner flow with injected offline
responses, and real capture/postcheck CLIs against private local HTTP monitoring
and SQLite fixtures. The latter removes exactly the two target consumers from
its broker fixture and checks both legacy workflows remain visible and inactive;
a subsequently reactivated legacy workflow fails the postcheck. **No real
deletion was tested or performed.** Real read-only broker INFO responses were
checked against monitor configs/identity and matched. The live runner's delete
branch remains subject to independent operational review.
