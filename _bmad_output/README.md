# BMAD output

Ticket-scoped execution artifacts live here (notes, checklists, evidence snippets).

## Closeout artifact convention

For every completed ticket, add one closeout file:
- Path: `_bmad_output/issue-<id>-execution.md`
- Starter template: `_bmad_output/templates/ticket-closeout.md`
- Scaffold helper: `ISSUE_ID=<id> mise run bmad:closeout-scaffold`

Required fields:
- Ticket id + title
- Scope completed (what changed / what did not)
- Verification evidence (commands/checks and outcomes)
- Links to issue + PR (and follow-ups if any)

Scaffold helper options:
- `ISSUE_TITLE='...'` to prefill the title
- `OWNER='...'` to prefill owner
- `OVERWRITE=1` to replace an existing closeout file

## Closeout artifact index

- `issue-38-execution.md` — closes out #38 via PR #39 (BMAD closeout standard)
- `issue-185-execution.md` — closes out #185 with verified strict-clean recovery evidence (no PR required)

Index append convention (for each new closeout file):
- `issue-<id>-execution.md` — closes out #<id> via PR #<pr> (<short scope note>)

## Verification checklist (closeout quality gate)

Before marking a ticket closed, confirm the closeout artifact includes:
- At least one concrete command/check with an explicit outcome (pass/fail or observed state).
- A direct state assertion for the ticket/PR pair (for example: issue closed, PR merged).
- Links to the exact issue and PR URLs (not just numbers).
- Any out-of-scope/deferred work called out explicitly (`none` when empty).
- Notes section completed with risks/caveats or `none`.

## Structured snapshot option (automation-friendly)

Use JSON mode when evidence needs to be consumed by scripts/tools (artifact generators, dashboards, checks):
- `python3 cli/bb.py repo-health --json`
- JSON includes `worktree_dirty` for explicit clean/dirty worktree checks.
- Add `--require-clean-worktree` when the command should fail on dirty trees.
- Shortcut task: `mise run repo-health:strict`.

## Timestamped artifact task (preferred for closeout evidence)

Use the dedicated mise task to generate standardized evidence files:
- `mise run repo-health:artifact`

Expected output pattern:
- `_bmad_output/evidence/repo-health-<utc-timestamp>.json`

Generated evidence files are runtime artifacts and should not be committed.
Use cleanup as needed:
- `mise run repo-health:cleanup` (remove all generated snapshots)
- `KEEP=5 mise run repo-health:cleanup` (keep newest 5)
- `KEEP=5 REPORT=1 mise run repo-health:cleanup` (JSON report with removed/kept counts and paths)
- `DRY_RUN=1 KEEP=5 REPORT=1 mise run repo-health:cleanup` (preview only; no file deletions)
