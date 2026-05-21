# Bloodbank — Agent Guide

The event backbone of the 33GOD ecosystem. Dapr runtime over NATS JetStream,
CloudEvents envelopes, AsyncAPI contracts, Apicurio for runtime schema lookup,
EventCatalog for human discovery.

## Stack

- **Broker:** NATS JetStream (durable streams: `BLOODBANK_EVENTS`, `BLOODBANK_COMMANDS`)
- **Runtime:** Dapr (pub/sub + state + secret stores defined under `compose/components/`)
- **Wire format:** CloudEvents 1.0 (JSON)
- **Schema home:** `bloodbank/schemas/` in this repo is the canonical
  source of truth. Holyfields keeps a transitional copy for downstream
  consumers; future re-extraction is appropriate once multiple independent
  consumers exist outside Bloodbank.
- **Schema registry:** Apicurio (read side); Bloodbank-local generators
  produce the SDKs (write side).
- **Discovery:** EventCatalog
- **Producers/consumers:** language-agnostic. Services in this repo are
  Python stdlib-only by design.

There is no in-process broker, no RabbitMQ, and no FastAPI publisher service in
Bloodbank itself. Production traffic flows through Dapr sidecars embedded
alongside each service, using publishers generated from the local
`bloodbank/schemas/` tree.

## Layout

| Path                | Role                                                              |
|---------------------|-------------------------------------------------------------------|
| `compose/`          | Self-hosted sandbox: NATS, Dapr placement, Apicurio, EventCatalog |
| `compose/components/` | Dapr component manifests (pub/sub, state, secret store)         |
| `compose/nats/`     | JetStream topology (`streams.json`) + init script                 |
| `cli/bb.py`         | Operator CLI (`doctor`, `trace`, `replay`, `emit`)                |
| `ops/bootstrap/`    | Pre-boot platform validation                                      |
| `ops/smoketest/`    | End-to-end smoke tests (NATS-direct, Dapr publish, subscribe, heartbeat, claude-events) |
| `ops/replay/`       | Operator-facing replay workflow                                   |
| `ops/trace/`        | Correlation/causation walkthrough                                 |
| `services/`         | Reference services that participate in the sandbox                |
| `adapters/`         | Migration scaffolds for legacy producers (blocked on Holyfields)  |

## mise tasks

| Task            | Purpose                                                  |
|-----------------|----------------------------------------------------------|
| `mise run up`           | Boot the core sandbox (NATS + nats-init)         |
| `mise run up:all`       | Boot every profile (heartbeat + claude-events + Dapr smoke) |
| `mise run down`         | Tear the sandbox down (`-v` removes volumes)     |
| `mise run doctor`       | `cli/bb.py doctor` — manifest-driven scaffold check |
| `mise run repo-health`  | `cli/bb.py repo-health` — read-only git/issue/PR/check snapshot |
| `mise run repo-health:json` | `cli/bb.py repo-health --json` — structured snapshot for scripts/tools |
| `mise run repo-health:strict` | `cli/bb.py repo-health --require-clean-worktree` — fail gate on dirty trees |
| `mise run repo-health:drift` | read-only drift snapshot (ahead/behind + modified/untracked + top-level buckets) |
| `mise run repo-health:artifact` | timestamped JSON evidence file under `_bmad_output/evidence/` |
| `mise run repo-health:cleanup` | remove generated artifacts; optional `KEEP=N`, `REPORT=1`, and `DRY_RUN=1` preview |
| `ISSUE_ID=<id> mise run bmad:closeout-scaffold` | scaffold `_bmad_output/issue-<id>-execution.md` from template |
| `ISSUE_ID=<id> SLUG=<slug> mise run bmad:worktree-bootstrap` | bootstrap isolated clean worktree from `origin/main` for ticket loops |
| `mise run bmad:pr-merge-safe -- <pr> [--no-reconcile-main]` | safe squash merge + merged-state verification + cleanup follow-ups + safe post-merge main reconciliation attempt |
| `mise run bmad:closeout-loop -- <pr> [--primary-repo <path>]` | unified closeout summary (merge+cleanup+drift evidence, JSON; defaults `PRIMARY_REPO` env then cwd) |
| `mise run bmad:closeout-loop:artifact -- <pr> [--primary-repo <path>]` | same as closeout-loop, plus timestamped artifact write to `_bmad_output/evidence/closeout/` |
| `mise run bmad:closeout-cleanup-summary -- [--evidence-dir <dir>] [--limit <n>]` | read-only summary of closeout artifact cleanup status fields for quick operator review (defaults to `_bmad_output/evidence/closeout`) |
| `mise run bmad:gh-readonly-status -- issue-view <id> \| pr-view <id> \| repo-view` | read-only JSON status helper with bounded retry for transient `gh` API connectivity errors |
| `mise run bmad:retrigger-pr-checks -- <pr> [--workflow ci.yml] [--dry-run]` | dispatch CI workflow for PR head branch without no-op commit retriggers |
| `mise run bmad:preflight-strict-clean -- [--repo <path>]` | strict-clean preflight gate; emits actionable JSON and fails fast when worktree is dirty |
| `mise run bmad:reconcile-main-divergence -- [--repo <path>] [--apply] [--limit <n>]` | detect/optionally reconcile patch-equivalent `main...origin/main` divergence with commit-side summaries |
| `mise run bmad:primary-recovery-check -- [--repo <path>]` | read-only divergence diagnostics + helper-availability check for primary checkout recovery |
| `mise run bmad:align-main-with-backup -- [--repo <path>] [--bundle-dir <path>] [--apply]` | backup-first helper to align diverged local `main` to `origin/main` (read-only by default) |
| `mise run bmad:recovery-artifact-cleanup -- [--repo <path>] [--bundle-dir <path>] [--keep-branches <n>] [--keep-bundles <n>] [--min-bundle-age-hours <h>] [--apply]` | cleanup helper for post-recovery backup branches + bundle artifacts (dry-run default, optional age gate) |
| `mise run bmad:reconcile-submodule-drift -- [--repo <path>] [--apply]` | detect/optionally reconcile submodule gitlink drift to superproject recorded commits (read-only default) |
| `mise run bootstrap`    | `ops/bootstrap/check-platform.sh` — pre-boot validator |
| `mise run validate:schemas` | Validate the local schema tree (`$id` uniqueness + `$ref` resolution + Draft 2020-12 check) |
| `mise run smoketest`    | NATS-direct event round-trip                     |
| `mise run smoketest:command` | NATS-direct command + reply round-trip      |
| `mise run smoketest:dapr`    | Dapr publish path                           |
| `mise run smoketest:dapr-subscribe` | Dapr publish → subscribe              |
| `mise run smoketest:heartbeat`      | Heartbeat producer/consumer end-to-end |
| `mise run smoketest:claude-events`  | Claude `bloodbank.v1.*` event round-trip |
| `mise run smoketest:bloodbank-naming` | Stdlib contract verifier (no Docker) for §14 sequence × {claude, copilot} + negative probes |
| `mise run smoketest:schemas` | `validate:schemas` + `smoketest:bloodbank-naming` chained for full schema-side coverage |
| `mise run smoketest:repo-health-cleanup` | local cleanup + strict worktree checks (default/KEEP/REPORT/DRY_RUN/error) |
| `mise run smoketest:bmad-closeout-scaffold` | local validation for closeout scaffold helper (required id/create/no-overwrite) |
| `mise run smoketest:bmad-closeout-loop` | local validation for unified closeout helper JSON/evidence fields |
| `mise run smoketest:bmad-merge-pr-safe` | local validation for safe merge helper JSON/cleanup follow-up fields |
| `mise run smoketest:bmad-merge-pr-preflight-guard` | local validation for merge helper strict-clean preflight enforcement + explicit bypass path |
| `mise run smoketest:bmad-retrigger-pr-checks` | local validation for PR check retrigger helper JSON contract (dry-run path) |
| `mise run smoketest:bmad-github-body-safety` | guardrail grep for risky inline `gh ... --body "..."` patterns in BMAD operator docs/scripts |
| `mise run smoketest:bmad-closeout-cleanup-summary` | local validation for closeout cleanup summary helper JSON output contract |
| `mise run smoketest:bmad-closeout-artifact-summary` | local validation for closeout artifact write path + summary visibility contract |
| `mise run smoketest:bmad-repo-health-retry` | local validation for bounded transient retry behavior in `cli/bb.py repo-health` gh read paths |
| `mise run smoketest:bmad-repo-health-helper-availability` | local validation for `repo-health` helper availability snapshot fields (`helper_local_exists`, `helper_on_origin_main`) |
| `mise run smoketest:bmad-gh-readonly-status` | local validation for bounded transient retry behavior in read-only issue/pr status helper |
| `mise run smoketest:bmad-preflight-strict-clean` | local validation for strict-clean preflight helper JSON/exit contract (clean pass + dirty fail) |
| `mise run smoketest:bmad-reconcile-main-divergence` | local validation for patch-equivalent `main` divergence detection/reconcile contract |
| `mise run smoketest:bmad-primary-recovery-check` | local validation for read-only primary checkout recovery diagnostics contract |
| `mise run smoketest:bmad-align-main-with-backup` | local validation for backup-first diverged-main alignment helper contract |
| `mise run smoketest:bmad-recovery-artifact-cleanup` | local validation for backup-branch + bundle cleanup helper contract |
| `mise run smoketest:bmad-reconcile-submodule-drift` | local validation for submodule gitlink drift reconcile helper contract |
| `mise run smoketest:hermes-runtime-hygiene` | local validation for Hermes runtime ignore contract (runtime state ignored, skeleton trackable) |
| `mise run smoketest:ops` | consolidated local operator reliability smoke checks (cleanup/scaffold/closeout-loop/merge-safe/merge-preflight-guard/retrigger-checks/github-body-safety/cleanup-summary/artifact-summary/repo-health-retry/repo-health-helper-availability/repo-health-idle-gate/repo-health-pilot-step-autoheal/gh-readonly-status/preflight-strict-clean/reconcile-main-divergence/primary-recovery-check/align-main-with-backup/recovery-artifact-cleanup/reconcile-submodule-drift/hermes-runtime-hygiene, fail-fast) |
| `mise run logs`         | Tail every Bloodbank container                   |

## BMAD baseline

- This repo is BMAD-initialized with a lightweight scaffold under `_bmad/` and `_bmad_output/`.
- Quickstart: read `_bmad/README.md` for ticket execution flow, then `_bmad_output/README.md` for closeout index + verification checklist expectations.
- For ticket-first work, use `_bmad/templates/ticket-execution.md` to track scope/steps/verification per issue.
- Ticket closure hygiene (ops/process tickets): include all three references before closing:
  - issue URL
  - merged PR URL
  - `_bmad_output/issue-<id>-execution.md` artifact path
- CI-failure triage (when a PR check goes red): capture a minimal evidence loop before next action:
  - failing check run URL
  - `gh run view <run-id> --log-failed` excerpt (error signature)
  - follow-up ticket URL when the fix is out of current PR scope
- For GitHub CLI reliability fallbacks (`projectCards` deprecation class), use `ops/bmad/github-cli-reliability.md` and prefer `gh ... --json` or `gh api` REST paths in automation loops.
- For shell-safe issue/PR markdown composition, use `ops/bmad/github-body-authoring.md` and prefer `--body-file`/file-backed REST payloads over inline `--body "..."`.
- For scriptable evidence capture, use: `python3 cli/bb.py repo-health --json` (includes explicit `worktree_dirty` signal plus helper availability fields).
- `repo-health` applies bounded retries for transient GitHub API connectivity errors on read-only `gh` status calls (`issue list`, `pr list`, `pr checks`).
- For direct automation reads of issue/PR/repo state, prefer `mise run bmad:gh-readonly-status -- issue-view <id>|pr-view <id>|repo-view` over raw `gh ... view`.
- For gate-style checks, add `--require-clean-worktree` to force non-zero exit on dirty trees.
- Before mutating loop actions (branch/commit/merge), run `mise run bmad:preflight-strict-clean` and treat non-zero as a hard blocker requiring hygiene routing.
- For non-mutating loop evidence on local drift state, use `mise run repo-health:drift`.
- If `main` is `ahead` and `behind` with patch-equivalent divergence, use `mise run bmad:reconcile-main-divergence` to confirm and optionally apply safe local reconciliation.
- If helper files are missing locally while `main` is diverged, run `mise run bmad:primary-recovery-check` then follow `ops/bmad/primary-checkout-recovery.md` before any destructive sync action.
- For backup-first canonical alignment, use `mise run bmad:align-main-with-backup -- --repo <path>` (read-only) then rerun with `--apply` only after review.
- After successful alignment verification window, run `mise run bmad:recovery-artifact-cleanup -- --repo <path>` (dry-run) before any cleanup apply; use `--min-bundle-age-hours` to protect fresh bundles.
- For persistent submodule gitlink drift (e.g., Hermes PM runtime), run `mise run bmad:reconcile-submodule-drift -- --repo <path>` for diagnostics first, then rerun with `--apply` only when the helper reports no non-drift worktree edits. The helper now retries with a forced checkout for the known runtime `state.db` overwrite blocker on `agents/hermes/pm/runtime`.
- `repo-health:pilot-step` may auto-heal strict-gate failures by applying the submodule drift helper once (`DRIFT_AUTOHEAL_ON_STRICT_FAIL=1` default); disable with `DRIFT_AUTOHEAL_ON_STRICT_FAIL=0` when manual intervention is desired.
- `bmad:pr-merge-safe` now attempts safe post-merge reconciliation by default; use `--no-reconcile-main` only when you explicitly need to defer reconciliation.
- For quick cleanup-status review across closeout artifacts, use `mise run bmad:closeout-cleanup-summary`.
- Runtime closeout evidence JSONs under `_bmad_output/evidence/closeout/` are operator-generated artifacts and intentionally git-ignored.
- If primary checkout is dirty/behind, use the dedicated clean-worktree automation path in `ops/bmad/clean-worktree-automation.md` (do not stash/discard unknown local changes).
- Local OpenClaw hook scratch path (`services/agent-hooks/openclaw/`) is intentionally treated as operator-local and excluded from repo tracking.
- Hermes PM runtime state under `agents/hermes/pm/runtime/` is operator-local; only intentional scaffold/docs should be tracked, and `.gitmodules` should keep `submodule.agents/hermes/pm/runtime.ignore=dirty` so runtime-local tracked-file edits do not dirty the primary checkout.
- Keep BMAD artifacts concise and ticket-scoped; avoid process bloat.

## Conventions

- **Event naming contract is `docs/event-naming.md`.** It is the single source
  of truth for `type`, subject, `kind`, allowlists, banned tokens, and where
  provider identity lives. Any conflict between that doc and code/config is
  a defect in the code/config.
- CloudEvents `type`: `bloodbank.v1.<domain>.<entity>.<action>` (5 tokens).
  Regex: `^bloodbank\.v[0-9]+\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$`.
- NATS subject: `bloodbank.<kind>.v1.<domain>.<entity>.<action>` (6 tokens),
  where `<kind>` ∈ `{evt, cmd, rpy}`. Stream filters: `bloodbank.evt.v1.>`,
  `bloodbank.cmd.v1.>`, `bloodbank.rpy.v1.>`.
- Legacy `event.>` / `command.>` / `reply.>` subject prefixes and 3-token
  types (`agent.session.started`, `copilot.tool.pre`, etc.) are **deprecated**;
  removal is tracked by the §16 migration tickets in `docs/event-naming.md`.
- Provider, CLI, and model identity live in the envelope `actor` field
  (`actor.cli`, `actor.provider`, `actor.model`) and never in `type`.
- Envelopes are CloudEvents 1.0 with `correlationid` and `causationid` on
  every message. Producers MUST set both. Events additionally carry
  `ordering_key`; commands carry `command_id`, `idempotency_key`, `delivery`.
- Schemas live in `bloodbank/schemas/` (see `docs/event-naming.md` §12).
  Bloodbank never invents an envelope shape outside that tree.
- Sandbox compose project name is `bloodbank`; network is `bloodbank-network`;
  container names are `bloodbank-*`.

## Anti-patterns

- No service-to-service calls that bypass the broker.
- No ad-hoc envelopes; every envelope conforms to a schema under
  `bloodbank/schemas/bloodbank/v1/<domain>/<entity>.<action>.v1.json`.
- No synchronous I/O in event handlers.
- No assumptions of a centrally-running publisher service — there isn't one.
- No provider/CLI/model names anywhere in `type` (claude, anthropic, copilot,
  github, openai, gemini, cursor, opencode, amazonq, codex, ollama, …).
- No `response` or `request` as the `action` segment — use the verb pair
  `received` / `sent` (e.g. `llm.response.received`, `llm.request.sent`).
- No imperative actions on `kind=event`; no past-tense actions on `kind=command`.
