# BB-34/TOLT-100 per-episode ownership — source candidate

Not accepted, installed or enabled. Independent SPEC then QUALITY belong to
the parent. No Forge source adoption is claimed. The two existing family
gates still default false; the installed router has neither this protocol nor
its shared path. No canonical event, payload, detector, stream, consumer,
credential or recurring clock is added.

## 1. Source authority and atomic fence (Forge owner must implement)

Source anchors at accepted dormant Forge prep `7bf9c7671daa7269a23fde67bd3f8f26463835f9`:
`skills/ltf-make-game/scripts/concept_alerts.py::{record,observe}`,
`concepts.py::transaction`, and
`lifecycle_events.py::{low_water_intent,source_intents,Outbox}`.
Source state path is Forge's existing
`.work/foundry/controller/concept-low-water.json`; source publication outbox
is `.work/foundry/controller/lifecycle-events.sqlite3`.
This candidate implements only the Bloodbank side. Tests model the required
source race; they do not prove the actual Forge implementation enforces it.

Keep `ltf.concept-low-water/1`: a complete count <=5, including zero, starts
one UUID4 episode; >5 rearms; unknown neither creates nor rearms.
Keep the existing episode and saved `bloodbank_intent` **byte-for-byte**,
including `source={notification_owner:direct,
publication_hold:bb34-per-episode-ownership-unaccepted}`. Mutable authority is
a sibling, never a rewrite of that historical proof.

Under the existing `concepts.transaction(root, "concept-notifications")`:

1. Operator-authorized activation writes an immutable barrier into
   `state["bloodbank_ownership_barrier"]`, alongside the existing `outbox`,
   in the same durable source-state replacement. Generate one UUID4
   `barrier_id`; snapshot **every already-existing episode ID** into sorted
   `excluded_episode_ids`, regardless of pending/sending/uncertain/delivered.
   Record the exact project, pool and destination. No timestamp boundary.
   Restart recovers this same barrier; never silently create/rotate one.
2. Only the existing `record` branch that actually mints a **future** episode
   may create `episode["bloodbank_ownership"]` at birth, in the same source
   write as its immutable intent. Local `source_control` fields:
   `schema=bloodbank.tolt-lowwater-source-control/1`,
   `admitted_barrier_id`, `episode_id`, `direct_claims=0`,
   `owner=direct`, `grant=null`. Absence is permanently historical, not
   permission to backfill admission.
3. Transfer re-reads the canonical episode under that same lock. Require the
   birth admission to this barrier, not excluded, `state=pending`,
   `direct_claims==0`, no `attempted_at`, receipt or sending/uncertain history,
   and an intact saved intent. Compute the grant from its original bytes.
   Atomically set sibling `owner=bloodbank` and `grant=<exact grant object>`.
   Source `write` must fsync the replacement **and parent directory** before
   returning. Failure to prove durability holds both export and publication.
4. Every direct claim must re-read this sibling **inside its existing
   pending->sending transaction**, require `owner=direct`, and increment
   `direct_claims` with the direct state/attempt stamp. A stale snapshot outside
   the lock cannot authorize send. Direct winning first permanently defeats
   transfer; fencing first permanently defeats direct.

No source lock is held during grant export, broker/HTTP calls or ACK reads.
The sibling fence never expires, reverts or retargets: crash, missing files,
publication failure, rejected import or lost ACK leaves Bloodbank ownership.
Sending/uncertain direct episodes remain direct **reconciliation-only**.
Never re-notify/backfill historical episode
`70f28232-7cc8-41a8-b251-188034cf2628` (already infra-delivered, receipt
`MF7dhRLqPrNi`). Historical direct notification remains authoritative.

## 2. Closed local control, path and transport

Machine schema: `services/ntfy-router/lowwater_control.schema.json`,
identifier **`urn:33god:control:tolt-lowwater:1`**, Draft 2020-12.
Every record and nested object is closed (`additionalProperties=false`).
These are administrative local records, NOT CloudEvents or new payload fields.

Proposed host directory:
`/var/lib/bloodbank/tolt-lowwater-ownership`.
Operator-provisioned read-only router bind:
`/run/bloodbank/tolt-lowwater-ownership`.
Configure `NTFY_TOLT_OWNERSHIP_DIR` to that container path and
`NTFY_TOLT_OWNERSHIP_UID` to the actual source-writer numeric UID; neither
has a configured source default. Do not use router UID10001 as source writer.
Source owns directories (0750) and files (0640); grant router GID10001 read
and directory search through an explicit group/ACL. No group/world write;
source mount read-only to router. Router writes only its existing UID10001
SQLite volume. The code checks expected ownership, regular files,
non-writable group/world modes, no symlink traversal within the configured
tree and a 600000-byte read limit; it never creates the shared source path.
Host root and explicit operator configuration are the trust boundary, not an
assertion inside an untrusted event.

Files, published by fsync+atomic replace+directory fsync **after source lock
release**, are immutable:

| File / record | Exact fields and types |
| --- | --- |
| `barrier.json`, `bloodbank.tolt-lowwater-barrier/1` | `barrier_id`: canonical UUID4; `project_slug`: `tower-of-lost-things`; `pool_id`: `curated-plane-novel`; `destination`: exact HTTPS endpoint/topic string; `excluded_episode_ids`: unique UUID4 array. Export from the durable source barrier, never infer it from dates. |
| Grant, `bloodbank.tolt-lowwater-grant/1` | UUID5 strings `grant_id`, `fence_id`, `event_id`; UUID4 strings `barrier_id`, `admission_barrier_id`, `episode_id`; exact `project_slug`, `pool_id`, `destination`; original `observed_at` string and integer `eligible_count` 0..5; `envelope_sha256`: lowercase 64 hex; `fence`: exactly `{owner:"bloodbank",direct_state:"pending",never_attempted:true,permanent:true}`. Both barrier IDs must equal the pinned barrier. |
| `grants/<grant_id>.json`, `bloodbank.tolt-lowwater-handoff/1` | `grant`: closed grant above; `envelope_body`: original saved UTF-8 JSON string, <=262144 characters (family input <=262144 bytes); `publication`: closed proof below. Body SHA is over the **exact saved bytes**, not a reserialization at receipt. The original source codec must match compact sorted JSON with ASCII escaping and finite JSON numbers. |
| `publication` | `kind`: `jetstream_puback` or `jetstream_readback`; `stream`: `BLOODBANK_EVENTS`; positive integer `sequence`; `message_id`: original semantic UUID5 event ID; exact low-water `subject`; `envelope_sha256`: same original body SHA. Source exports only its first durably recorded proof; repeated readbacks cannot rewrite this handoff. |

The source publishes the original envelope with **`Nats-Msg-Id=event.id`**
and awaits JetStream PubAck, or performs exact durable stream readback matching
ID, subject and bytes. Source outbox proof is retained before handoff export.
Router requires this bound source-supplied proof; it does not independently
contact the broker or claim its import ACK is a broker receipt.

Real producer identity is constrained to `urn:33god:cli:ltf-concept-alerts`,
producer `ltf-concept-alerts`, service `lost-things-forge`, actor exactly
`{type:service,agent_id:ltf-concept-alerts}` and source episode record_ref.
Original envelope time equals original observed_at; original count/time/ID
must match the grant. The **canonical installed validator** remains the sole
event-schema enforcement path.

Grant visibility can follow NATS arrival or precede its observation by the core
subscriber. Export always follows the durable source fence and publication
proof. A fence->publish->export crash recovers from the existing source
episode/sibling and outbox: re-export the same bytes/IDs and first proof.
Source must retain handoffs and retry import/read status until durable router
acceptance, including after publication is already acknowledged. A published
event is NOT evidence the ephemeral core consumer saw it.
Persist a returned ACK as `episode["bloodbank_import_ack"]` (separate closed
ACK object, not part of saved intent/source proof) or in the existing derived
outbox. **Retain barrier, source fence and handoff files permanently in v1**:
ACK stops import retry pressure, not retention or authority. No GC/revoke API
is introduced. The existing source drain/recovery invocation must include
fenced low-water events lacking import ACK even if their publish status is
already `published`; ordinary publish-pending selection is insufficient.

## 3. IDs and ACK

`NS = UUID5(UUID_NAMESPACE_URL, "https://33god.dev/control/tolt/lowwater/1")`
= **`b23a872e-915f-5e6a-b5e8-8659dd54aa67`**.
For every derivation, UUID5 name is JSON of the ordered array below, compact
separators `(",",":")`, ASCII escaping; no sort operation on array elements:

```text
fence_id = UUID5(NS, ["fence", project_slug, pool_id, episode_id])
grant_id = UUID5(NS, ["grant", barrier_id, project_slug, pool_id, episode_id,
                     event_id, destination, envelope_sha256])
ack_id   = UUID5(NS, ["ack", grant_id_or_null, handoff_sha256_or_null,
                     status, reason])
```

Event ID remains the accepted canonical semantic UUID5; no remint. Grant and
handoff SHA256 cover compact, sorted-key, ASCII JSON of their complete objects.
Request and event disposition digest retains the existing router codec and
stored request bytes; no credentials are saved in grants/requests.

An ACK (`bloodbank.tolt-lowwater-ack/1`) has exactly:
`ack_id`, nullable `grant_id`/`event_id`, nullable `handoff_sha256` (null when
input could not be decoded), `status`, `reason`, boolean `durable_import`,
nullable `delivery_state`, plus `schema`. Returned only after FULL-synchronous
SQLite COMMIT. ACK is retained in `lowwater_ack`.
For a repeated ACK ID, the returned object is the exact original retained
object, including its delivery-state snapshot; it is never rebuilt with newer
delivery state. Current delivery is exposed only by the separate status fields.

- `accepted`: immutable grant/body/request imported atomically into existing
  disposition ledger; `durable_import=true`. Does **not** mean HTTP occurred.
- `duplicate`: exact accepted import already durable; same authority,
  `durable_import=true`. Existing delivery outcome is not reset.
- `rejected`: invalid/missing/stale/untrusted control or held legacy/gate-closed
  disposition; `durable_import=false`. Fence still forbids direct fallback.
- `conflict`: immutable mismatch; retained permanently; `durable_import=false`.

Explicit bounded source/operator readback, **after a separate approved
installation**, uses the existing container, no HTTP and no new daemon:

```sh
docker exec bloodbank-ntfy-router python /app/lowwater_ownership.py --import-pending
docker exec bloodbank-ntfy-router python /app/lowwater_ownership.py --status <grant_id>
```

Status separates original import ACK, current `ownership_status` and current
delivery state/attempts/HTTP ID. A prior accepted ACK cannot override a later
conflict. Source must read both. Exact repeated ACK IDs can be deduplicated.
Unknown grant returns `not-imported`, never assumed accepted.
Grant acceptance rechecks the pinned barrier's active state, identity and exact
digest **inside the same BEGIN IMMEDIATE transaction** as grant/disposition/ACK
insertion or duplicate acceptance. A concurrent barrier conflict that commits
first therefore defeats late acceptance: the attempted grant/disposition is
retained as conflict and its ACK cannot report durable import acceptance.
Status reads one SQLite snapshot and derives current ownership from both the
grant and pinned barrier, including any legacy late-accepted row. A previously
committed accepted ACK remains immutable historical evidence, never overrides
current permanent conflict, and never re-enables the direct source path. The
separate HTTP claim barrier guard remains required and unchanged.

Bounded import enumeration persists its current **finite filename snapshot and
next position** in SQLite `lowwater_import_scan`, keyed by configured source
root. Fresh one-shot CLI calls, restarted drains and concurrent readers reserve
successive pages in a short BEGIN IMMEDIATE transaction; no grant-body read or
import is performed while that reservation lock is held. Every selected file
still goes through current trusted-path, closed-schema, exact-body and immutable
conflict checks. Accepted/delivered files remain present and are revalidated
on later rounds, not deleted or permanently skipped. Malformed first-page
files consume only their own bounded slots. New filenames, including ones
sorting before the current position, wait for the next finite round. A crash
after reservation may defer that page until its next round, never lose its
retained controls or grant authority by itself. The metadata contains filenames
only, not cached event/grant acceptance; no control-wire IDs or clock are added.

Offline synthetic known vector (never import into real runtime):
barrier `b614d990-146c-4cd5-8442-c6a643149631`;
episode `44c9d238-0347-48b4-8820-1b4565e73065`;
event `811cf31c-af98-5062-ab60-5d466964e7d3`;
body SHA `189f7399d149c356eca803e5173ac0a4b1bb91580d2a3a9c8daffccd45bac104`;
fence `08afa085-e0b8-5c0b-a019-d19cc16f8db1`;
grant `7000c6ed-d18a-5f0c-bb0b-dbcd074cbe65`;
handoff SHA `fa7f9ac331b9d5450d0ed6b08d99759645d9d34b6cc8370fff8a15116edf3fe9`;
first accepted ACK `ee85944b-bcf1-59b0-a6a4-8aaed1f0fe4b`.
The evidence bundle includes the full labelled synthetic object and vector.

## 4. Ledger, failure and ordering rules

Existing `delivery` remains the **one** disposition per event/destination.
Additional tables register immutable barriers/grants, rejected conflict digests
and ACKs, not a second delivery engine. Unique grant ID, semantic event ID,
episode and fence prevent transfer to multiple destinations. Barrier changes
are durable conflict, not an automatic rotation/revocation mechanism.
After path trust and closed-schema validation, pinned barrier bytes are checked
before current-config destination rejection. A shape-valid destination change
therefore latches conflict through restoration/restart. Untrusted, undecodable
or schema-invalid files cannot supply invented bytes to that comparison.
Same event/different body, request, grant, first publication proof or destination
conflicts survive restart; original bytes are never replaced.

With explicit ownership-path configuration and both gates true:
- Live event-before-grant: persist the immutable event/request as `pending`,
  reason `awaiting-owner-grant`, without HTTP. Missing/unreadable control leaves
  this recoverable hold. Excluded episodes become legacy suppression.
- Handoff-before-event or missed core delivery: atomically import the
  **same saved canonical envelope** plus grant/proof/request into that ledger.
  ACK import first, then normal delivery through existing startup/stats drain.
  This recovery adapter supplements, not replaces, durable publication/audit.
- Bounded delivery selection persists a per-destination `delivery_scan`
  cursor in the same SQLite database. Each finite rowid round fixes its upper
  boundary before selecting at most `drain_limit` due pending rows. Progress
  survives restart, skips past blocked pages without deleting/promoting holds,
  and wraps after that finite round; continuous new tail arrivals cannot
  indefinitely postpone revisiting earlier holds. Imported-but-unreadable
  authority cannot monopolize the first page or strand later family work.
  A crash after selection may defer that page until its next round, never
  resets its intent or authorizes HTTP. No new clock or control-schema fields.
- Only imported matching authority can let low-water reach HTTP claim.
  Current trusted barrier/handoff must still be readable and match. Corruption
  fails closed; a parsed immutable change is durable conflict. No file expiry
  or timeout grants ownership. No new poller is started.
- Existing `suppressed` records are NEVER promoted. Pre-upgrade pending
  records without the new waiting marker/registered grant are legacy holds,
  including when posting is disabled or retry time is not due.
- Global gates closed at first observation/import cause terminal suppression,
  not deferred enablement. Temporarily disabling gates does not make an
  existing grant authorize POST; both remain required at delivery.

Without ownership-path configuration, unconditional low-water suppression is
unchanged. Non-low-water handling, TestFlight eligibility, formats/priorities,
generic buckets and mirrors are unchanged. Preview remains conservative:
low-water render never claims an available owner gate or send eligibility.

Existing SQLite BEGIN IMMEDIATE claim permits one active HTTP attempt.
Only positively known preaccept rejection may bounded-retry the same stored
request; ambiguous/lost HTTP ACK, sending expiry and late responses remain
`uncertain`/reconcile-only, never a blind second acceptance. `accepted` is ntfy
HTTP acceptance, not device presentation. Rejected/conflicted ownership never
restores direct.

## 5. Staged plan — NOT executed

This is a narrow TOLT unblock: dogfood canonical Bloodbank publication/audit,
reuse Forge's episode and liftable durable outbox, and put an opt-in
Adapter/Strategy at the ownership boundary rather than build an orchestration
platform. Source transaction/fence plus recoverable export is the transactional
outbox boundary; router SQLite is the HTTP disposition boundary.

1. Parent independently SPEC then QUALITY this exact source candidate.
   Forge owner separately adopts/reviews source birth admission, source fsync,
   fence/direct claim race, sibling-aware outbox eligibility and retained export/
   ACK recovery. Existing `source_intents` exact historical proof is not rewritten.
2. Separately authorize router packaging/install and operator-owned read-only
   mount/UID/ACL provisioning. Keep both global gates false and no source barrier
   active. Inspect installed canonical/schema hashes and persistent SQLite.
   This candidate deliberately does not add a live Compose mount or set flags.
3. After source/router owner acceptance and separate enablement authorization,
   configure the path/UID, verify the old historical episode stays excluded and
   its direct receipt is not imported. Verify no pre-barrier episode can transfer.
   Choose exact destination once. Any rejected import is a stop/reconcile gate,
   not permission to use direct as fallback.
4. Explicitly authorize both global delivery gates and source barrier activation
   as a coordinated operation before admitting any future grants. Snapshot
   legacy episodes under the source lock. No grant is exported while globals
   are closed: doing so would permanently suppress that intent.
5. Wait for a **real next qualified episode** from the existing detector.
   Prove source fence + real immutable outbox + JetStream PubAck/exact readback
   + Candystore exact ID + durable import ACK/current ownership + one router
   disposition/no mirrors. HTTP acceptance and phone proof are separate.
   Only then call the replacement proven for transferred episodes; historical
   direct delivered/uncertain/active episodes retain reconciliation authority.

No rearm, fake game/claim, synthetic live fact, topic/phone test, new clock,
global restart, install or cutover is authorized by this source acceptance.
