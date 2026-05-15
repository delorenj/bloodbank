# Issue 166 — execution closeout

## Ticket
- Issue: #166
- Title: Automate safe post-merge reconciliation of patch-equivalent main divergence
- Owner: hermes (pilot)

## Scope completed
- Extended `ops/bmad/merge_pr_safe.py` to attempt post-merge reconciliation by default via `reconcile_main_divergence.py --apply`.
- Added explicit defer override `--no-reconcile-main` and machine-readable `post_merge_reconcile` report contract (`attempted`, `applied`, `status`, `reason`, `helper`).
- Updated deterministic merge helper smoke coverage to include post-merge reconcile contract behavior (blocked, bypass/defer, and default attempted-apply path).
- Updated operator task/docs wiring (`AGENTS.md`, `mise.toml`, `ops/bmad/clean-worktree-automation.md`) to reflect default reconciliation behavior.

## Out of scope
- Retrofitting auto-reconcile invocation into other non-merge operator entrypoints.

## Verification evidence
- `bash ops/smoketest/smoketest-bmad-merge-pr-safe.sh` → `PASS`
- `bash ops/smoketest/smoketest-bmad-merge-pr-preflight-guard.sh` → `PASS`
- `python3 -m py_compile ops/bmad/merge_pr_safe.py` → success

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/166
- PR: <url>
- Follow-up tickets: none

## Notes
- Reconciliation remains guarded by helper safety checks; apply is only effective when branch is `main` and divergence is patch-equivalent.
