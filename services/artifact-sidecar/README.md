# Project Artifact Sidecar + Vault Projector

Two independent bus participants that turn qualifying agent-produced content
into durable, project-attributed artifacts in the operator's vault:

```
agent CLI hooks ──▶ bloodbank.evt.agent.tool.completed ──▶ artifact-sidecar ──▶ bloodbank.evt.project.artifact.created ──▶ artifact-vault-projector ──▶ vault
                     bloodbank.evt.conversation.turn.completed      │                                                                              Projects/<dir>/<YYYY-MM-DD>/<title>
                                                                 content-addressed store (sha256 blobs, fsync, no-clobber)
                                                                 sqlite state (revisions, processed ledger, holds, outbox)
```

No direct calls between the two services — the bus is the only channel, per
repo law. The sidecar never reads the vault; the projector never scrapes
repos; neither executes content.

## What an "artifact" is (and is not)

Durable user-facing deliverables: diagrams, charts, documents, notes, idea
files, BMAD artifacts. NOT artifacts (deliberately): source code, tool
stdout, edit diffs, subagent reports, ambient repo docs, config files.
Qualification is a positive structural match on a real producer event —
never content guessing, never inference from ambient files. When bytes
qualify but attribution is ambiguous, the sidecar HOLDS them and reports; it
never guesses a project.

## Discovery adapters (and their honest limits)

| basis | real producer shape | status |
|---|---|---|
| `agent_tool_write` | Claude Code `Write` publishes `arguments.file_path` + full `arguments.content` (verified live in Candystore, 2026-10-16); Hermes `write_file` publishes `arguments.{path,content}` or, in older events, only `payload.tool_input.{path,content}` (read as a fallback) | works; text media only — binary extensions (png/pdf/…) NEVER qualify from inline strings (no fabricated binaries); denied locations (.env/config/secrets/scratch) refused before persistence |
| `conversation_reply_mermaid` | fenced mermaid blocks in `conversation.turn.completed` `data.reply_text` | implemented + tested; identity is thread-scoped and digest-named (no project-wide merge); **live producers currently publish reply_text empty** — inert until a producer populates it |
| `conversation_reply_delivered` | reply text with explicit producer marker `data.delivered`/`user_facing` truthy or non-empty `deliverables` | implemented + tested, **inert**: no producer sets the markers today. Producer change needed (services/agent-hooks/clients/hermes.py `shape_data` for turn.completed): set `delivered: true` when the native payload marks the reply a deliverable |
| `project_activity_report` | `bloodbank.project.activity.recorded` (activity-report skill): `data.project.slug` + `data.report.{markdown,html}`; own durable `bloodbank-artifact-sidecar-activity` | works (live on the real james-brennan reports); markdown canonical + html render twin; identity project+audience+window.start; slug must be enrolled in the pjangler registry |
| `operator_resolution` | operator maps a held capture to a project via `--hold-resolve` | works |

Unsupported, reported not widened (live observations, 1000-event sample):
- opencode `write`/`edit` — no inline content in arguments (path only).
- Edit-family tools anywhere (old/new string pairs, no full content).
- Bash/terminal outputs, Monitor, read results — process, not deliverables.
- Binary artifacts: no blob-reference producer adapter exists; inline
  strings can never honestly carry PNG/PDF bytes. **Gap, not claimed done.**
- Rendered mermaid: no hook exposes a rendered twin yet; the schema carries
  `artifact.render` and the projector writes the pair (render extension from
  its own media type), but the sidecar will not invent renders.

## Attribution

Project comes only from the canonical pjangler registry
(`http://127.0.0.1:8764/v1/registry`, read-only, refreshed on a bounded
period with last-good kept on failure): the source event's
`working_directory` is resolved against registry `repo_path`s, deepest match
wins, `__registry_status[project].status != ok` entries are skipped.
`status: planned` is NOT an exclusion. **Git-identity disambiguation** then
runs: a linked worktree (e.g. `…/33GOD/bloodbank-artifact-sidecar`)
attributes to the repo its git common-dir belongs to (bloodbank), never the
33god umbrella it sits inside; a foreign nested repo is held. Ambiguous or
missing attribution ⇒ durable hold keyed by
`hold_id = sha256(source_event_id, logical_path, sha256)` (provenance kept);
`--hold-show` / `--hold-resolve <hold_id> <slug>` / `--hold-publish
<hold_id>` close the loop with basis `operator_resolution`.

## Durability & idempotency

Ordering inside the sidecar: **bytes → atomic (state+ledger+outbox) →
publish → ack**.

1. Blob written to the content-addressed store first (fsync + no-clobber
   link; corrupt preexisting blobs refused, never trusted).
2. Artifact row + processed-source ledger + outbox row committed in ONE
   transaction (a crash can never strand an artifact whose event was never
   enqueued, and never enqueues an event with no artifact row).
3. Outbox row carries the fully validated envelope (schema + invariants
   BEFORE the bus can ever see it).
4. Awaited JetStream publish with `Nats-Msg-Id` = stable event id
   (server-side dedupe collapses outbox replays). Only a successful publish
   marks the row published — "enqueued" is never labeled "published".
5. Publication retries on the IDLE TIMER (not only incoming events),
   exponential backoff, bounded 20 attempts; exhausted rows keep `--status`
   exit nonzero until `--retry-outbox` re-arms them.
6. Stale replay of an OLD source event after a newer revision is a no-op
   (processed ledger) — never a reverted revision.

Delivery policy: NEW durables get `DeliverPolicy.NEW` (no seven-day dump);
backfill is the explicit `ARTIFACT_SIDECAR_BACKFILL=1` /
`ARTIFACT_VAULT_BACKFILL=1` opt-in. Enable the projector durable BEFORE
artifact events flow (see docs).

`artifact_id = uuid5(project slug, project-relative logical path)`: same
logical file + same bytes = duplicate (no event); same path + new bytes =
revision+1; different path = independent artifact.

## Security posture

- **No execution of incoming content.** Nothing is evaled, rendered, or run;
  mermaid sources are stored and copied as text. The only subprocess is
  `git rev-parse` (fixed argv, timeout) for attribution identity.
- **Read allowlist.** The sidecar's only local reads are its own store and
  state. It never reads the vault, never scans config/cache/secret dirs; a
  defensive denylist (.ssh/.config/.cache/.env/config//secrets//scratch/
  .git/…) applies to every capture — inline included — on the
  project-relative path, before persistence.
- **Path traversal/symlink refusal.** Containment via resolved-path
  commonpath + per-component symlink checks on the ORIGINAL components (a
  within-root symlink cannot hide). Store refs are strict lowercase-hex
  sha256 (traversal/uppercase refused pre-open); fd-based O_NOFOLLOW walks;
  bounded reads; blobs 0600, fan-out dirs 0700, state dbs 0600.
- **Size bounds.** 64 MiB capture cap (schema-enforced), 200k reply scan cap.
- **No network fetches of content.** Only NATS (bus) and the loopback
  registry endpoint. Content refs are sha256 strings, never URLs.
- **No secrets in events.** Path hygiene is schema + invariant enforced.

## Vault projector rules

- Mapping is **explicit config** (`vault-mapping.json`): slug → directory
  relative to the vault root. `bb` → `Projects/Bloodbank` (canonical per
  DeLoDocs/AGENTS.md; the historical `Projects/33GOD/BloodBank` is never
  touched or merged). Unmapped slug ⇒ durable hold (`--holds`,
  `--hold-resolve-map`), never a guessed directory, never lost.
- Full canonical schema + invariants validated BEFORE any store read or
  write; malformed envelopes dead-letter (durable hold).
- Date folder = the artifact's creation time (`data.origin.created_at` — the
  SOURCE event's time), converted to **America/New_York**; fallback
  envelope time.
- Never overwrites at any race: dirfd+O_NOFOLLOW traversal (planted
  symlinks refused), O_EXCL tmp + fsync + no-clobber hard link, dir fsync.
  Conflicting bytes get a DETERMINISTIC `-<sha8>` suffix; the ACTUAL path is
  recorded; crash-after-file-before-DB replays to the identical path (no
  duplicate).
- Source AND render digests + declared lengths verified pre-write; missing
  render = retryable error (never half-projected); render extension from its
  own media type.
- Cannot loop: it publishes nothing.

## Operating

```bash
# local tests (98 checks: schema, invariants, discovery, path safety, store
# hardening, pipeline idempotency, outbox retry/exhaustion, holds, worktree
# attribution, projector adversarial cases, fake-transport runtime contract)
python3 services/artifact-sidecar/test_sidecar.py

# isolated-broker runtime tests (docker nats:2.11-alpine, random port,
# never the sandbox bus)
python3 services/artifact-sidecar/test_broker.py

mise run smoketest:project-artifact-contracts   # offline suite, chained in smoketest:schemas

# sidecar
python3 services/artifact-sidecar/main.py --status            # exit 1 on exhausted/unresolved
python3 services/artifact-sidecar/main.py --hold-show
python3 services/artifact-sidecar/main.py --hold-resolve <hold_id> bb
python3 services/artifact-sidecar/main.py --hold-publish <hold_id>
python3 services/artifact-sidecar/main.py --retry-outbox

# projector
python3 services/artifact-vault-projector/main.py --plan --mapping ops/systemd/artifact-vault-projector/vault-mapping.json --vault-root /tmp/vault
python3 services/artifact-vault-projector/main.py --once ...   # drain backlog, exit
python3 services/artifact-vault-projector/main.py --holds      # unmapped/malformed holds

# SYNTHETIC live integration probe (real bus, real registry, REAL created
# artifact file, throwaway vault, probe-owned durable only) — tests the bus
# contract, NOT the production hook path
python3 ops/live/artifact-live-probe.py --project bb
```

Install/disable templates: `ops/systemd/artifact-sidecar/` and
`ops/systemd/artifact-vault-projector/` (NOT installed by the repo; parent
operator copies, reviews, enables). Templates install under
`%h/.config/systemd/user/` with explicit paths; the interpreter needs
python3.11+ and `nats-py` (`python3 -m pip install --user nats-py`) — verify
with `python3 -c "import nats"` in the unit's environment before enabling.
Both services are off unless explicitly started — nothing here hooks into
agent CLIs or auto-activates.

Design context: `docs/project-artifact-sidecar.md` (in this worktree).
