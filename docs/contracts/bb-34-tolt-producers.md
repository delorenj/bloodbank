# BB-34: five-fact producer contract candidate

Status: **candidate for independent SPEC then QUALITY; not adopted or installed**.
Native owner `01a1b8ba-60c4-7a61-bccb-f1bd982d5e51`; branch
`feat/bb-34-tolt-contract-first`, base `db9fd9f26baeaee512073bf4054d5598c058cee3`.
November 7, 2026 America/New_York. No producer, board, router, mobile or release
changes are authorized by this document.

## Reconciled decision and sources

The exact persisted scout record is
`/home/delorenj/.claude/projects/-home-delorenj-code-tolt/0614d092-5e9a-4ee8-bd33-288e88f18c69/workflows/wf_0f5bd884-cdb.json`.
Read `result.findings` and `result.contract`: status **completed**, timestamp
**2026-11-08T01:08:00.668Z** (November 7 20:08 EST), four agents **done**.
The accompanying `agents/hermes/pm/runtime/workers/BB-34/scout-reconciliation.md`
resolves the prior unavailable-scout blocker. Scout names were proposals, not
existing schemas or approvals. Bloodbank PM now selected five candidate facts:

| Type | Subject | Schema |
| --- | --- | --- |
| `bloodbank.game.implementation.started` | `bloodbank.evt.game.implementation.started` | `schemas/bloodbank/game/implementation.started.json` |
| `bloodbank.game.pack.released` | `bloodbank.evt.game.pack.released` | `schemas/bloodbank/game/pack.released.json` |
| `bloodbank.game.concept_pool.breached` | `bloodbank.evt.game.concept_pool.breached` | `schemas/bloodbank/game/concept_pool.breached.json` |
| `bloodbank.game.item.created` | `bloodbank.evt.game.item.created` | `schemas/bloodbank/game/item.created.json` |
| `bloodbank.project.app_launch.recorded` | `bloodbank.evt.project.app_launch.recorded` | `schemas/bloodbank/project/app_launch.recorded.json` |

Five fact/entity addresses avoid discriminator-dependent consumer semantics.
This unblocks TOLT production, dogfoods Bloodbank and reuses small authoritative
source transitions rather than building a new orchestration platform. The
previous delivery-observation schema/union is abandoned. Local `concept_pool`
naming is retained; no established bus schema duplicates the existing local
Forge low-water fact. Deployment completion remains artifact installation or
publication, not app launch. No existing producer adoption is claimed.

Source paths below are relative to Forge
`/home/delorenj/code/tolt/lost-things-forge` or TOLT
`/home/delorenj/code/tolt/tower-of-lost-things`. Those repos remain read-only.
The previous slice verified primary/sole-worker `job.py` and `concept_alerts.py`
byte parity; this invocation uses completed scout findings and narrow source
reads, not another discovery/worker dispatch.

| Owner / child | Actual adoption seam, not a claim it is implemented |
| --- | --- |
| Forge / TOLT-100 implementation | `skills/ltf-make-game/scripts/job.py` `cmd_phase`: first durable build phase `running` after preparation/live-tree/upstream certification. Existing `claim.id` from `_reserve_from_pool` is canonical across retries. Reservation/init/PM dispatch/revision is not commencement. A persisted transition does not prove code was written before a crash. |
| Forge / TOLT-100 low stock | `skills/ltf-make-game/scripts/concept_alerts.py` `record` and `observe`, called by `job.py` intake. Extend the existing `ltf.concept-low-water/1` episode/outbox; no second count trigger. `concepts.py` pool is the complete production-authorized, unclaimed novel collection; held/incomplete results are excluded or unknown. |
| TOLT / TOLT-98 pack | `tools/register.mjs` publish; `tools/catalog.mjs` immutable PCK/catalog swap; `tools/content.mjs` version-list refresh; `tools/player.mjs` player publication; `ops/site/hub/play.js` exact-version loading. Add post-publication readback only after all required artifacts are actually playable. Forge `job.py` publication-check is corroboration, not upload/registry-only readiness. |
| Forge/TOLT / TOLT-101 item | TOLT `tools/register.mjs` ready-vs-draft batch and verified content-main push; Forge `foundry/cron/items-daily.sh`; TOLT `tools/items.mjs`/`tools/forge/item.mjs` candidate registration. Emit only ready item + required art landed on remote content main. `plan.made`, draft/art attempt, candidate registration, vetting and live publication are distinct. |
| TOLT / TOLT-99 startup | `game/core/app.gd` actual `_ready()`/APP_START. Native bridge must supply actual iOS bundle values. `game/core/diag.gd` diagnostics are local only; `tools/build-stamp.mjs` Linux Android-derived code is NOT final native iOS identity. `tools/testflight.mjs` Mac build selection and `tools/ios.mjs` final archive can differ. |

## Payload contract (all schemas revision 1)

The five JSON Schemas are precise, closed at each payload-object level. All
inherit the canonical CloudEvents base. Required envelope fields include exact
four-token `type`, five-token `subject`, `domain`, `kind=event`, UUID `id`,
actual producer `source`/`producer`/`service`/`actor`, `ordering_key`, JSON
content type, matching `.v1` and `/versions/1` handles, `correlationid`, explicit
nullable `causationid`, original `time` and `data`. No command delivery fields
are invented. Provider/CLI/model identity belongs in `actor`, never the address.

Game data shared across only the four game facts:
`schema_version:1`, `project_slug:string slug`, `observed_at:UTC RFC3339`,
`summary:string[1..200]`, `correlation_id:UUID`, `cause_event_id:UUID|null`,
`provenance:{basis,record_ref,source_commit}`. `record_ref` is a logical
`urn:` or `repo://` URI, never an absolute file path; `source_commit` is
40/64 lowercase hex or null for runtime observations. Game lineage twins must
match the envelope; roots are null, and self-causation is forbidden. Reuse an
actual causal chain when one exists; a Plane ticket or local claim is not a
bus parent. Persist one root correlation before attempts if no upstream exists.

| Family | Required specific fields and meaning |
| --- | --- |
| implementation | `implementation_id:UUID` = existing claim ID; `job_id:string[1..160]`; `concept_id:UUID`; `milestone=build_started`; `game_kind=new_game`; provenance basis `forge_build_phase_running`. Once per canonical implementation, not retry attempt. |
| low water | `pool_id:slug` (local candidate `curated-plane-novel`); existing `episode_id:canonical UUID4`; `episode_schema=ltf.concept-low-water/1`; `eligible_count:integer[0..5]`; `threshold=5`; `complete=true`; basis `forge_low_water_episode`. Original episode count/created time are frozen. |
| item | `item_id:slug`; `item_name:string[1..160]`; `content_status=ready`; `item_sha256` and `art_sha256`: exact committed record/art bytes; nullable `request_id` matching `req-<digits>`; `registry_status=not_observed/candidate/approved/rejected/live`; basis `forge_item_landed`, non-null content authority commit. `not_observed` is honest; ready never implies approved/live. |
| pack | `title:string[1..160]`; `pack:{id:slug,version:integer>=1,sha256:lowercase64}` (tested PCK bytes); `source_ticket:UUID|null`; `source_job:string[1..160]|null`; `changelog:string[1..4000]`; `release_kind:initial/update` (owner's actual initial-vs-revised content classification, not catalog/readback revision); `catalog_revision:string[1..160]`; `game_commit:40/64 hex`; non-null content `provenance.source_commit`; basis `tolt_pack_readback`. |

Pack also requires `playable_url`: verified HTTPS exact `/play/<id>?v=<version>`
without credentials, fragment or fallback version. Query parsing retains blank
values and is strict: extra keys (even blank), malformed fields, empty or
duplicate v values are rejected. The raw query must be literal `v=<integer>`;
percent-encoded key/value aliases and any `#` delimiter (even empty) are rejected.
A 200 page is NOT proof:
`play.js` can silently fall back. `verification` requires true
`catalog_verified`, `exact_version_verified`, `artifact_verified`,
`player_assets_verified`, `load_verified`. `channels` is 1–3 unique entries
`{channel:web/private_app/testflight,verified:true,evidence_ref,native_build}`;
verified web playback is required by this publication contract. Native channel
entries require `{bundle_id,app_version,build_number}`; web uses null.
Advertise only actually verified surfaces. Store/TestFlight uses `bundled_only`
(`tools/ios-pack-refresh.mjs`, `game/core/catalog.gd`); a newly published web
pack does NOT appear in an already-installed TestFlight build.

### App launch has independent project data, no game payload requirements

Required data: `schema_version:1`, `project:{slug}`, original UTC `observed_at`,
`summary`, `installation_id:canonical UUID4`,
`installation_id_origin=persisted_csprng_uuid4`,
`native_build:{bundle_id,app_version,build_number}`,
`native_identity_source=ios_bundle`, `final_native_identity_verified=true`,
`bundle_manifest_sha256:lowercase64`, `installation_verified=true`,
`bundle_verified=true`, `native_boot_verified=true`,
`provenance:{basis:tolt_native_first_startup,record_ref}`.
App launch uses envelope lineage only, with canonical correlation/cause UUIDs,
explicit null root cause and self-cause rejection. It does not depend on shared
game data, a pack ID, game job, `project_slug` or payload lineage twins.

`native_build.app_version` is actual **CFBundleShortVersionString**;
`build_number` is actual **final CFBundleVersion** read from the installed iOS
bundle, not a Linux/Android `Diag.build_info().code` or intended release value.
Both are dotted-numeric strings; bundle ID is the installed native identifier.
Native app version and pack integer version are unrelated axes.

`channel=testflight/app_store/private_app/unknown` and
`testflight_eligible:boolean` are explicit. TestFlight requires eligible=true
and `channel_evidence:{basis:verified_testflight_runtime,evidence_ref:<logical>}`.
Unknown or other channels require eligible=false. Evidence basis is closed by
channel: testflight permits only `verified_testflight_runtime`; app_store and
private_app permit only `verified_other_distribution`; unknown permits only
`unverified` or `sandbox_receipt_only`. The shared `native_build` definition is
reused by launch and native pack channels for the same dotted-numeric native
app-version shape; no game payload fields are inherited by app launch. `unverified` or
`sandbox_receipt_only` evidence is not sufficient for TestFlight; sandbox can
also mean other distribution. Unknown stays unknown, not silently upgraded.
Source owner must verify actual runtime provenance; schema-valid assertions
alone are not authenticity proof. No actual-device TestFlight evidence is
claimed here. Only eligible TestFlight observations qualify for the intended
TestFlight notification; unknown observations must never be formatted as such.

Generate a cryptographically random installation UUID once, persist separately
from gameplay saves, and retain it through updates. Uninstall normally resets
it and can count again; backup/restore or copied app data may preserve/clone it.
This is per installation/build, not per human or physical device forever.

## Exact stable identities and known synthetic vectors

Keep namespace `uuid5(uuid.NAMESPACE_URL, "https://33god.dev/bloodbank/game")`
= `d8d05b87-993a-5550-9d73-a647951c7fac`, also for the independent project
launch fact (namespace reuse does not impose game fields).
Encode the following ordered arrays using Python-equivalent
`json.dumps(parts,ensure_ascii=True,separators=(",",":"))`; UUID5-hash that
string in this namespace. UUID spellings are lowercase canonical with hyphens;
pack version is a JSON integer. No timestamps, attempts, display labels,
notification topics, catalog revisions or channels enter the ID.

| Fact | Array | Ordering |
| --- | --- | --- |
| implementation | `[type,project_slug,implementation_id]` | `game:<project>:implementation:<implementation_id>` |
| pack | `[type,project_slug,pack.id,pack.version,pack.sha256]` | `game:<project>:pack:<pack.id>` |
| low water | `[type,project_slug,pool_id,episode_id]` | `game:<project>:concept_pool:<pool_id>` |
| item | `[type,project_slug,item_id]` | `game:<project>:item:<item_id>` |
| app launch | `[type,project.slug,installation_id,native_build.bundle_id,native_build.app_version,native_build.build_number]` | `project:<slug>:installation:<installation_id>` |

The exact synthetic input envelopes are
`services/agent-hooks/tests/tolt_contract_fixtures.py`; frozen expected vectors:

| Fixture | Expected UUID5 |
| --- | --- |
| implementation | `47eb32b3-f0d2-516d-a7f9-b77d32a7c681` |
| ready pack | `aee4ab32-7326-578d-8b2b-b73e77d2bd2f` |
| low water | `14b91329-a325-5640-bcb7-6dc398f12e8c` |
| item | `47ee22cb-0503-5f7a-adde-0f629c8bb307` |
| app launch | `38ef5256-6902-5bfb-baa1-6762ec34656b` |

One immutable pack/version/hash creates one release fact across repeated
readbacks, channels, unrelated catalog revisions and rollbacks. Freeze the
first verified channel set and envelope; later channel additions do not create
another release or overwrite it. If separately notifying channel availability
is wanted, that needs a future distinct fact, not mutable re-release bytes.
Pack IDs/versions must not be recycled for changed bytes; such a source conflict
must be held visibly even though different hashes mathematically yield IDs.
Same item ID never remints because of art/name/status/content-commit changes.

## Source durability and ntfy cutover obligations

Production/gameplay stays fail-open while retaining pending facts. Atomically
persist the source transition plus immutable envelope/outbox intent, or retain
recoverable durable intent reconciled from authoritative state before marking
emitted. Local persistence failure must be visible, not swallowed as success.
Network I/O is outside source locks. Original observation time and original
immutable envelope `time` remain separate from broker/Candystore receipt time;
offline delivery does not rewrite either. Replays use identical saved bytes/ID.

Adapt existing `services/artifact-sidecar/sidecar.py` SQLite outbox and
`main.py` awaited JetStream publish precedent, not its artifact registry coupling.
Send `Nats-Msg-Id=id`; advance only after matching BLOODBANK_EVENTS PubAck/sequence
or verified durable stream readback. Raw core PUB/PONG is not durability proof.
Retain failed/pending/exhausted rows, use bounded backoff (precedent 20 attempts)
and explicit retry of exhausted state. No new clock. Durable uniqueness ledgers
must outlive JetStream's finite duplicate window. Same ID/different envelope is
a surfaced conflict; Candystore ON CONFLICT alone does not diagnose it.

Reuse Forge's existing `ltf.concept-low-water/1` episode: complete <=5 including
initial zero creates UUID4 once; complete >5 rearms; unknown neither means zero
nor rearms. Never add another threshold worker. Extend the existing episode
identity and frozen count/time to a bus outbox. Keep its ntfy
pending/sending/uncertain/delivered state intact. Direct infra-to-Bloodbank
cutover MUST prohibit dual pushes and retroactive notifications of already
delivered/uncertain episodes. Unknown send outcomes are reconcile-only, never
automatically resent. An active low episode is not a fresh alert at migration.

TOLT-99 must persist the native first-start fact before async delivery, survive
process death/offline/update, and use a narrow durable server ingress/outbox
with the same uniqueness scope. Remove client pending only after durable server
acceptance; acknowledge duplicates successfully. No generic envelope endpoint,
app broker credentials, embedded shared secrets or claimed ingress adoption.

**Topic decision:** Bloodbank PM selected dedicated `https://ntfy.delo.sh/tolt`.
Monitoring-pm **MONI1 acceptance/configuration/iOS wake are pending**, coordinated
by durable command `678cf9dc-b9aa-5656-9921-dcf63f409c1f`. This is not supported/live
yet and no HTTP call was made. Runtime routing remains configurable. Normal
priority **3**, low-stock **4**. One family disposition/semantic-ID-and-destination
ledger; suppress domain-entity/domain mirrors. Do not reuse deploys or deployment
semantics for wake eligibility. HTTP acceptance, Candystore durability and actual
iOS presentation are three separate proofs. Router/install remains deferred.

## Reproducible source-only verification and handoff

Use saved complete envelopes, NOT `bb emit` (which generates a new UUID4):

```sh
python3 services/agent-hooks/tests/test_tolt_contract.py
python3 cli/bb.py verify-envelope --file saved-envelope.json
mise run smoketest:schemas
python3 cli/bb.py contract --json
git diff --check
```

`verify-envelope` checks the stdlib contract on full input; the focused suite
also calls `validate_envelope` for full JSON Schema validation. No intentionally
red CLI acceptance test remains. Fixture outbox tests exercise the existing
local adapter using fake failure/ack callbacks; this is not live broker proof.
No broad ASM/production-Redis suite is run.

The ignored worktree Plane marker was copied from primary only after primary
marker, primary/worktree `.project.json` and `role.yaml` all matched canonical
board `10d06f8d-c110-4ce5-beaa-0914534b090a`. The existing plane-binding test reads
that marker directly; `.scripts/80-registry.sh` also cats it, and
`.scripts/42-ticket-provider.sh` mirrors the canonical board into it. No source
fallback was changed to hide failure. This runtime marker stays ignored.

Exact outputs, command/exits, complete diff including untracked source, source
hashes and manifest are in the authorized BB-34 spool `reconcile-evidence/`.
The bounded `reconcile-report.md` names the fingerprint for independent SPEC
then QUALITY. No self-approval, commit/push/main/PR or production work occurred.
Remaining risk/acceptance belongs to source owners: real source-transaction
crash recovery and conflict holds, authenticated/truthful native/channel
provenance, device persistence, one-notification cutover, MONI1 topic/wake
acceptance, installed identities and actual PubAck/Candystore/ntfy/device proof.
Local schemas are authority; no invented registry-sync prerequisite.
