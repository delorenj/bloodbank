# BB-34 exact-family route — unreviewed source candidate

This seam affects only:

- `bloodbank.game.implementation.started`
- `bloodbank.game.pack.released`
- `bloodbank.game.concept_pool.breached`
- `bloodbank.game.item.created`
- `bloodbank.project.app_launch.recorded`

The corresponding `bloodbank.evt.*` transport subjects are also intercepted
when the envelope is missing/misstates its type. Family input never falls
through to generic buckets, digest overflow, domain mirrors or deploys copies.
Every other type keeps the existing wildcard route and its policy unchanged.
The original 69 router tests remain; family tests use synthetic saved contracts
and fake POSTs only.

## Default-closed cutover

Both `NTFY_TOLT_ENABLED` and `NTFY_TOLT_CUTOVER_ACCEPTED` default to **false**.
Both must explicitly be true before an otherwise eligible non-low-water input can send.
These switches require actual owner acceptance; their existence does not
establish MONI1 configuration, topic authentication/subscription or iOS wake.
The selected destination is `tolt`, configurable by `NTFY_TOLT_TOPIC`.
Generic/domain/deploys topics are refused for this seam.

Valid observations received while either gate is closed are durably
`suppressed`. They are not automatically promoted when configuration changes.
This is an observed-intent rule, not an epoch or historical-event guarantee:
core NATS cannot observe events missed while offline. It does not discover or
replay producer history.

**App launch alerts require both `channel == "testflight"` and
`testflight_eligible is True`.** Canonically valid `unknown`, `app_store` and
`private_app` launches remain useful events, but are not eligible for this
notification. They are persistently `suppressed` with
`app-launch-not-testflight-eligible`, retaining the immutable envelope, request
and digest. Gate, topic or configuration changes do not promote a held intent;
changed bytes under an existing event/destination identity remain a conflict.

Arrival handling and saved-intent delivery apply the same predicate after
canonical validation. Startup/existing-tick drain also scans a bounded page of
pending launches, even while posting is disabled or backoff is not yet due.
The cursor advances past eligible future TF rows so they cannot indefinitely
hide older ineligible intents. Existing pending non-TestFlight rows become
suppressed before any HTTP claim; eligible TestFlight rows retain normal gates,
retry timing and dedup. There is no new clock or automatic suppression release.

**Low-water delivery is unavailable in this candidate, regardless of flags.**
Every canonically valid low-water input is persistently suppressed with
`lowwater-owner-episode-gate-unavailable`, retaining its existing episode ID
and immutable envelope. This includes a late first observation after cutover
and later replays of an already held event. There is no timer/epoch inference,
new eligibility payload field, operator bypass or second detector.

The closed contract does not say whether Forge's direct `infra` notification
was delivered, uncertain or still active before cutover. A future producer-owned
durable per-episode gate must establish sole notification ownership and prevent
dual pushes or retroactive alerts for delivered/uncertain/active legacy
episodes. It must not automatically release this ledger's suppression records.
Until that separate contract/owner acceptance exists, low water stays closed.
Its priority-4 formatter can be tested without enabling delivery.

## Canonical validation and packaging

`CanonicalValidator` loads the actual
`<NTFY_FAMILY_VALIDATOR_ROOT>/services/agent-hooks/core/validate.py` and calls
its full `validate_envelope`, not just name/shape assertions. It requires that
the module's supported schema lookup resolves to that root's `schemas/`.
There is no ambient `core` import fallback or duplicated payload-schema
implementation. Missing module, schemas or jsonschema fails family routing
closed without changing non-family routing.

The standalone Compose build context deliberately changes from the service
directory to the repository root, with
`services/ntfy-router/Dockerfile.dockerignore` admitting only the router,
canonical core Python files, schemas and naming marker. Runtime/credentials,
Git history and other services are not build inputs. The image packages the
canonical tree under `/opt/bloodbank` and pins its schema override there;
root-owned canonical files are read-only to the application user.

The named `tolt-delivery-state` volume mounts `/var/lib/ntfy-router`.
The Dockerfile creates that directory owned by **UID 10001**, mode 0700.
SQLite database mode is 0600. This has source-level tests, not an image/volume
installation proof. A previously provisioned volume with wrong ownership must
be repaired through the approved install procedure; no runtime chown/init
daemon is introduced.

No build, install or service restart is part of this source candidate.
An approved install must build from the updated primary tree, inspect the
installed validator/schema/image identity and prove UID 10001 can write the
mounted ledger. Merely testing this worktree does not prove installed parity.

## Durable intent and HTTP outcomes

SQLite stores one immutable canonical JSON envelope + JSON ntfy request and
their SHA-256 digest per **(event ID, destination URL/topic)**. Authorization
headers/tokens are not part of that saved request. JSON whitespace/key-order
differences normalize to the same intent; different content under the same
identity becomes a retained `conflict`, without replacing the first bytes.
Formatter/config changes that produce a different request likewise conflict
rather than silently rewriting pending work.

SQLite `BEGIN IMMEDIATE` claims one row as `sending` with a unique token before
HTTP. No database connection/transaction is held across the network await.
Concurrent callbacks/instances cannot claim the same row twice. Delivery
states are:

| State | Meaning / automatic action |
| --- | --- |
| `pending` | Valid, authorized intent waiting for a bounded attempt/backoff. |
| `sending` | Claimed before HTTP; never stolen/retried as pending. |
| `accepted` | HTTP 2xx; status and ntfy message ID, if returned, are retained. Not phone presentation. |
| `uncertain` | Timeout, ambiguous transport/5xx, expired claim or late response. Reconcile-only, no automatic resend. |
| `exhausted` | Retry limit or terminal rejection; retained, no automatic rearm. |
| `conflict` | Same identity/different intent or corrupted saved intent; retained, never sent again automatically. |
| `suppressed` | Closed cutover or missing low-water episode authority; terminal, not retroactively promoted. |

429 rejection and a transport-proven preaccept failure use bounded exponential
backoff/Retry-After, with at most **5 attempts** by default (hard maximum 20).
A destination-wide pause prevents other pending family rows from immediately
spending the same rejected destination budget. The drain takes at most 10 rows
per invocation. Exhausted rows are not discarded.

The `Receipt.preaccept` transport boundary can represent a proven preaccept
5xx rejection, covered with fake transport. The real HTTP adapter deliberately
does **not** infer that any generic 5xx happened before acceptance: it leaves
those outcomes uncertain. It only marks 429 and connection-establishment
failure as definitely preaccept. Read/write timeouts, lost responses and
unclassified 5xx must never trigger a blind resend.

The existing startup path and existing stats tick drain pending rows and
expire claims. No new clock/task/stream is created. A crash leaves a sending
claim until its bounded lease expires; then it becomes uncertain, never a
fresh attempt. Late callbacks cannot overwrite uncertainty or a conflict.
Receipt metadata is retained where available, including a response arriving
after its deadline. Uncertainty is intentionally not resolved automatically.
There is no new operator retry/reconciliation mutation API in this slice.

Per-state counts appear in the existing stats line, and every family arrival
logs only its disposition—not its body, URL, token or exception text.
The accepted ledger is restart-persistent, not a bounded in-memory cache.
Changing the configured destination is a new dedupe scope and requires owner
cutover, not a promise of suppression across all topics.

## Presentation

All family messages use one JSON POST to the configured ntfy root with one
configured topic. Normal priority is **3**; low-water formatting uses **4**.
Implementation shows actual claim/job/concept IDs. Pack shows its exact integer
version, PCK digest, initial/update classification and verified surfaces/native
builds. Item mint explicitly distinguishes ready content from registry status.
Eligible TestFlight startup shows persisted installation ID and actual native
bundle/marketing version/final iOS build. Non-TestFlight preview text may describe
its channel honestly, but preview eligibility is false and no alert is delivered.
Original observed time and event ID are shown without rewriting the envelope.

Only the canonically validated pack URL can become a button. An additional
notification policy admits HTTPS/443 links only on `NTFY_TOLT_LINK_HOSTS`
(default `tolt.delo.sh`); a nonapproved host gets no action/click and is not
copied into the body. No arbitrary source/summary URL is forwarded. Titles and
display fragments collapse literal escaped newlines and control characters.
No untrusted display value becomes an HTTP header.

`--render` for these types is validation/format preview only: it creates no
ledger and sends nothing, and explicitly shows closed delivery gates.
It is not delivery/cutover evidence.

## Verification and remaining acceptance

```sh
python3 -m unittest discover -s services/ntfy-router -p 'test*.py' -v
python3 services/agent-hooks/tests/test_tolt_contract.py
python3 -m unittest discover -s services/agent-hooks/tests -p 'test_validate*.py'
mise run smoketest:schemas
git diff --check
```

Use the local schema root when explicitly testing a worktree; do not point the
installed service at an unreviewed checkout. Tests cover the five formatters,
four deliverable types plus unconditional low-water hold, both cutover gates,
missing/malformed/validator-unavailable input, no mirrors, restart/concurrent
dedupe/conflict, bounded known-preaccept retries, uncertainty and late callbacks,
safe links/text, fake HTTP receipts, packaging source and non-family regressions.

No NATS publish/PubAck, producer catchup, Candystore write/read, actual ntfy
POST, phone wake or exactly-once external delivery is proven. Producer source
outboxes and actual JetStream acknowledgement/readback remain a separate
durability boundary. MONI1 acceptance/config/wake, producer-owned low-water
episode cutover, independent SPEC then QUALITY, approved installation and live
receipts remain outstanding. No accepted schema/validator/contract doc changed.
