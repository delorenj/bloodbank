# Project Artifact Sidecar — Design

**Status:** live (deployed 2026-10-17). Two `systemd --user` units run from
the canonical checkout: `bloodbank-artifact-vault-projector.service`
(enabled first) and `bloodbank-artifact-sidecar.service`, rendered by
`ops/systemd/render-install.py --install`. The sidecar unit has a drop-in
(`~/.config/systemd/user/bloodbank-artifact-sidecar.service.d/env.conf`) with
`ARTIFACT_SIDECAR_CAPTURE_ROOTS=%h/code` and
`ARTIFACT_SIDECAR_BACKFILL_SINCE=2026-10-16T18:17:21Z` (the moment the
sidecar was requested; applied once, when the durable was created). The
vault mapping lives in `~/.config/bloodbank/artifact-vault-mapping.json`
(`bb`->Projects/Bloodbank, `33god`->Projects/33GOD,
`greeninbetween`->Projects/green-in-between, `holocene`->Projects/Holocene,
`flume`->Projects/Flume, `james-brennan`->Projects/james-brennan; add a row per
approved project). First backfill:
8 `bloodbank.project.artifact.created` events (6 greeninbetween, 2
holocene), all projected to `Projects/<dir>/2026-10-16/`; Candystore
ingests them (`bloodbank.evt.>`). Capture gaps still open: Hermes cwd was not the project until the
hermes-agent session-cwd fix is running (events from before it hold),
`reply_text` is never present on real turn events, Bash/heredoc and Edit
writes, attribution by file rather than cwd. Closed since: `project.activity.recorded`
reports (second durable, see Discovery) and Hermes `write_file` inputs at
`data.payload.tool_input` (fallback in `discovery.tool_arguments`). Until the
other gaps close, the writes seen are Claude/opencode/Hermes full-file
writes inside a `_bmad-output/` tree (or carrying a delivery marker), plus
activity reports.
Two live-only bugs found at deploy (no-candidate events were nak'd and
starved the consumer; a subdirectory cwd was held because git's relative
common-dir was resolved against the toplevel) are fixed and tested. The
`ops/live/artifact-live-probe.py` script is a **synthetic** integration
check.

## Contract

`bloodbank.project.artifact.created` /
`bloodbank.evt.project.artifact.created` — schema-first:
`schemas/bloodbank/project/artifact.created.json`, entity `artifact`
registered in the validator allowlist, invariants in
`assert_project_invariants` (docs/event-naming.md §11.4 artifact block).

Payload carries **references, not bytes, not paths**:

- `project`: `{slug, name}` copied from the canonical pjangler registry.
- `artifact`: stable `artifact_id` (uuid5 of slug + logical path),
  monotonically increasing `revision`, `title`, `media_type`, `sha256`,
  `bytes`, `content: {storage: content-addressed, ref: sha256:<hex>}`, and
  optional `render` twin (different digest; same logical artifact).
- `origin`: `detected_by`, `basis` (`agent_tool_write` |
  `conversation_reply_mermaid` | `conversation_reply_delivered` |
  `project_activity_report` | `operator_resolution`), `qualification_reason`, `source_event
  {type,id,time}` — envelope `causationid == source_event.id` enforced —
  `session {producer,cli,thread_id,invocation_id}`, and `created_at` (the
  SOURCE event's time; the projector dates by this so backfill lands on the
  true day).

Invariants (stdlib, fail-loud): content.ref == `sha256:`+sibling sha; render
digest differs; causation binding; basis allowlist; RFC3339 created_at;
no-absolute-path hygiene across all of `data`.

## Durability and delivery contracts (the parts reviews care about)

- **Atomic state:** artifact head + processed-source ledger + outbox row
  commit in ONE sqlite transaction. A crash can never strand an artifact row
  whose event was never enqueued (the old two-step commit could).
- **Never "published" until the transport says so:** the pipeline reports
  `enqueued`; `published` is set only after the awaited JetStream publish
  returns. Startup catch-up drains through the REAL transport (the old
  `drain_outbox(lambda env: None)` that marked rows sent without publishing
  is gone). `Nats-Msg-Id` = stable event id, so server-side dedupe collapses
  outbox replays.
- **Retries:** outbox retries on the idle timer (not only on incoming
  events), exponential backoff (2s base, 1h cap), bounded at 20 attempts;
  exhausted rows keep `--status` exit nonzero until the operator runs
  `--retry-outbox`.
- **Replay safety:** a replayed OLD source event after a newer revision is a
  no-op via the processed-source ledger (never a reverted revision); same
  bytes at head is a duplicate; new bytes revision+1.
- **Delivery policy:** NEW durables are created with `DeliverPolicy.NEW`
  (no seven-day vault dump). Backfill over retained history is the explicit
  `ARTIFACT_SIDECAR_BACKFILL=1` / `ARTIFACT_VAULT_BACKFILL=1` operator
  choice at first start. `ARTIFACT_SIDECAR_BACKFILL_SINCE=<RFC3339 with
  zone>` is the bounded form (`DeliverPolicy.BY_START_TIME`; wins over
  `BACKFILL=1`). Like every delivery policy it applies ONLY when the durable
  is first created and is ignored (logged as `bound`) once it exists.
- **Two durables, one process:** tool/turn events stay on
  `bloodbank-artifact-sidecar` (filter subjects untouched). Activity reports
  ride a SECOND durable, `bloodbank-artifact-sidecar-activity`
  (`ARTIFACT_SIDECAR_ACTIVITY_DURABLE`, `""` disables), filtered to
  `bloodbank.evt.project.activity.recorded`, with its own first-creation
  delivery policy (`ARTIFACT_SIDECAR_ACTIVITY_BACKFILL[_SINCE]`, each falling
  back to the primary setting, so the drop-in's `BACKFILL_SINCE` covers it).
  Chosen over editing the live durable's filter subjects because a changed
  filter does not backfill the new subject (the cursor is already past it)
  and it puts the proven consumer at risk. Both consumers share one state db,
  one registry and one outbox; outbox drains are serialized in-process.
- **Enablement order matters:** with NEW delivery, enable the PROJECTOR's
  durable before artifact events start flowing (or accept skipping them);
  sidecar-first then projector requires the projector backfill opt-in.

## Attribution

- Registry containment (deepest repo_path wins) **then git-identity
  disambiguation**: a linked worktree (e.g. `…/33GOD/bloodbank-artifact-sidecar`)
  attributes to the repo its git common-dir belongs to (bloodbank), never the
  33god umbrella it physically sits inside; a directory whose repository
  belongs to no enrolled project is held, never guessed.
- Holds are durable rows keyed by `hold_id` = sha256(source_event_id,
  logical_path, sha256) — provenance is kept (same bytes from two sources
  are two holds). Resolve with `--hold-resolve <hold_id> <slug>`, publish
  with `--hold-publish <hold_id>`.

## Discovery (narrow, structural — no ambient scraping)

- `agent_tool_write`: tool.completed with path + FULL inline content AND an
  EXPLICIT user-facing signal — a producer delivery marker
  (`data.delivered` / `data.user_facing` truthy, or non-empty
  `data.deliverables` / `data.attachments`) or a path inside a
  project-configured producer output tree. Defaults cover both BMAD
  spellings in this fleet (`_bmad_output/` — what Bloodbank actually uses —
  and `_bmad-output/`), user-facing media extensions only;
  `evidence/`, `logs/`, `scratch/`, `tmp/` components are excluded under
  those trees even if an operator widens the globs (process exhaust, not
  deliverables). Generic README/config/docs writes with no signal do NOT
  qualify — they are process. Binary extensions (png/jpeg/gif/webp/pdf)
  NEVER qualify from inline string content; genuine binaries arrive only
  through delivered-file capture (below). Denied content locations (.env,
  config/, secrets/, scratch/, .git/, …) are refused before persistence,
  inline or not, judged on the project-relative path.
- `conversation_reply_mermaid`: the fenced mermaid blocks of a reply,
  identity scoped thread+turn-wise and named by block digest — two turns in
  one thread discussing the same diagram chain revisions on one artifact;
  different threads are different artifacts.
- `conversation_reply_delivered`: reply text AND explicitly delivered local
  files, ONLY when the producer marked the turn user-facing (same marker
  set). Delivered local files arrive as `MEDIA:<absolute path>` tokens in
  reply_text or `path`/`file` entries in `deliverables`/`attachments`, and
  are read through the strict capture policy (artifact_lib.read_file_bounded):
  denylist fragments first, then containment under OPERATOR-ALLOWLISTED
  output roots (`ARTIFACT_SIDECAR_CAPTURE_ROOTS`, empty by default — opt-in
  only), lexical symlink refusal on every component, O_NOFOLLOW open,
  regular-file check, size bound, digest from the actual read bytes. This
  is the ONLY path to genuine binary artifacts (PNG/PDF): real bytes from
  disk, never an inline string. No remote fetch of any kind; `.hermes` is
  denylisted even when rooted (the agent cache is not a delivery source).
  Text deliverables (SVG/mermaid/markdown) delivered by path work the same
  way. The Hermes adapter (`services/agent-hooks/clients/hermes.py`)
  promotes `delivered`/`user_facing`/`deliverables`/`attachments` from the
  native payload when present, and (operator policy, 2026-10-17) sets
  `delivered: true` itself on every completed Hermes turn with a non-empty
  reply except `platform=subagent` — a Hermes reply reaches the user
  (Telegram, CLI, cron, Bloodbank-dispatched). So every substantive Hermes
  reply, and every `MEDIA:` file it names under the capture roots, becomes a
  `conversation_reply_delivered` artifact. Other producers still need an
  explicit marker; without one nothing is read.
- `project_activity_report`: a `bloodbank.project.activity.recorded` event
  (the activity-report skill; `data.report.{title,raw,markdown,html}`). The
  report names its own project, so there is NO cwd attribution: the slug in
  `data.project.slug` is looked up in the pjangler registry (not enrolled =>
  observable `skipped`, nothing persisted, no hold — a hold would also lose
  the html twin). The report **markdown is the canonical artifact** and the
  report's self-contained **html is the `artifact.render` twin** (projected as
  `activity-<audience>-<window.end YYYYMMDDTHHMMZ>.md` and
  `….render.html`); a report lacking markdown falls back to html, then to raw
  text. **Identity** is project + audience + `window.start`
  (`inline/activity/<audience>/<start>`): re-emitting the same window with
  different bytes is revision+1 on one artifact id, a different window or
  audience is a different artifact (a `cap_24h` window slides with the run
  clock, so a re-run later is a different window; `previous_report` windows
  start at the previous end and re-render onto the same id). Duplicate
  detection compares the canonical markdown digest only: a re-render that
  changes only the html is not a new revision. `generator.dry_run=true`
  (nothing pushed to the portal — every report today) is recorded in the
  qualification reason but does not disqualify. Consumed on its own
  durable, see below.
- `operator_resolution`: an operator mapped a held capture to a project.
- Hermes write shape: `agent_tool_write` reads the tool inputs from
  `data.arguments` and, when that is absent or empty, from
  `data.payload.tool_input` (real Hermes `write_file` events before the
  adapter promoted them carried only the latter). The delivery gate,
  attribution and denylist apply unchanged.
- There is no ambient repo-wide markdown/config sweep under any basis; a
  prompt merely mentioning a diagram never authorizes reading files — the
  output reference is a positive signal only together with the delivery
  marker.

## Vault projection safety

- Projector validates the FULL canonical schema + invariants BEFORE any
  store read or write; malformed envelopes are durable dead-letter holds
  (never acked-and-lost, never retried forever). Unmapped slugs are durable
  holds with `--holds` / `--hold-resolve-map` CLIs — never a RAM set, never
  a guessed shadow directory.
- VaultWriter walks project/date components via directory fds with
  O_NOFOLLOW (planted symlinks refused, not followed); files land via
  O_EXCL tmp + fsync + atomic no-clobber hard link; directories fsynced. A
  competing human writer can never be overwritten at any race: conflicts
  get a deterministic `-<sha8>` suffix, and the ACTUAL chosen path is
  recorded in the projection state.
- Source AND render digests + declared lengths are verified pre-write; a
  missing render blob is a retryable error (never half-projected); the
  render file extension comes from its own media type (mermaid source
  `.mmd`, svg render `.render.svg`).
- Crash between file and DB record replays to the identical deterministic
  path — no duplicate.
- Timezone America/New_York via `ARTIFACT_VAULT_TZ`; mapping `bb` →
  `Projects/Bloodbank` is explicit operator config (ops/systemd/
  artifact-vault-projector/vault-mapping.json); no other mapping is guessed.

## Content store

Refs are strict lowercase-hex sha256 (traversal/uppercase/non-hex refused
before any filesystem op); fd-based walks with O_NOFOLLOW; bounded reads;
preexisting blobs digest-verified (corrupt bytes refused, never trusted);
no-clobber link writes with dir fsync; store dirs 0700, blobs 0600; state
dbs chmod 0600.

## What this implementation does NOT do (concrete limits + gaps)

- **Binary capture is delivered-file only**: genuine PNG/PDF bytes are read
  exclusively from explicitly delivered local paths under operator
  roots (`ARTIFACT_SIDECAR_CAPTURE_ROOTS`, opt-in). There is still NO
  blob-reference producer adapter (tool.completed carrying a
  content-addressed ref with no local read) — the delivered-file adapter
  reads local disk; a remote/blob path does not exist.
- **Delivered-file capture is off until the operator roots it**: with no
  `ARTIFACT_SIDECAR_CAPTURE_ROOTS`, MEDIA: tokens and deliverable paths are
  refused (observable skipped dispositions), never silently dropped.
- **No mermaid RENDERING**: a render twin travels only when a real producer
  supplies one.
- The projector consumes `bloodbank.evt.project.artifact.created` only; the
  sidecar consumes tool/turn events only; no loop.
- Registry refresh is bounded-periodic in the sidecar runtime
  (`ARTIFACT_SIDECAR_REGISTRY_REFRESH_S`, default 300s, last-good kept on
  failure). Registry payloads are validated (slugs, repo_paths) at load.
- **Production hook path not verified by any test here**: the synthetic
  probe covers the bus contract; the real
  agent-hooks → bus → sidecar chain needs the parent's live check. (The
  canonical Hermes adapter → publisher → sidecar → projector chain IS
  covered offline by EndToEndHermesTests against the exact native
  shell_hooks payload shape.)

## Files

```
schemas/bloodbank/project/artifact.created.json     schema
services/agent-hooks/core/validate.py               artifact entity + invariants
services/agent-hooks/clients/hermes.py              native payload promotion (tool_input/result, markers)
services/artifact-sidecar/testdata/                 REAL Candystore events (hermes write_file without
                                                    arguments; james-brennan activity report)
docs/event-naming.md                                §7 entity row, §11.1 bucket, §11.4 block
docs/project-artifact-sidecar.md                    this document
services/artifact-sidecar/artifact_lib.py           store, path safety, registry, git identity,
                                                    bounded delivered-file reads
services/artifact-sidecar/discovery.py              adapters (structural, deterministic; explicit
                                                    delivery gate; delivered-file capture)
services/artifact-sidecar/sidecar.py                state, pipeline, atomic outbox, holds
services/artifact-sidecar/main.py                   runtime + CLI (status/hold-*/retry-outbox)
services/artifact-sidecar/test_sidecar.py           149 adversarial + contract + e2e tests
services/artifact-sidecar/test_broker.py            isolated-broker runtime tests (docker nats)
services/artifact-vault-projector/main.py           projector runtime (explicit mapping, holds)
ops/smoketest/smoketest-project-artifact-contracts.py  mise task (chained)
ops/fixtures/project-contracts.v1.json              artifact fixture
ops/systemd/artifact-sidecar/*.service              template (not installed)
ops/systemd/artifact-vault-projector/*.service + vault-mapping.json
ops/systemd/render-install.py                       renderer/installer (--dry-run default, no
                                                    install without --install; verifies the
                                                    interpreter imports nats+jsonschema and
                                                    rewrites repo/python paths)
ops/live/artifact-live-probe.py                     SYNTHETIC live integration probe
```

## Test commands

```
python3 services/artifact-sidecar/test_sidecar.py    # 149 tests, offline
python3 services/artifact-sidecar/test_broker.py    # 6 tests, isolated docker nats
mise run smoketest:project-artifact-contracts       # same suite as mise task
python3 ops/systemd/render-install.py --dry-run     # render + verify units, print only
```
