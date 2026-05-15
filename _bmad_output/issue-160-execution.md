# Issue 160 — execution closeout

## Ticket
- Issue: #160
- Title: Add explicit strict-clean preflight helper for BMAD operator loops
- Owner: hermes (pilot)

## Scope completed
- Added strict-clean preflight helper `ops/bmad/preflight_strict_clean.py` that wraps `repo-health --require-clean-worktree` and emits actionable JSON (`ok`, `worktree_dirty`, `blocking_reason`, errors).
- Added deterministic contract smoke test `ops/smoketest/smoketest-bmad-preflight-strict-clean.sh` covering both clean-pass and dirty-fail paths.
- Wired helper + smoke task into `mise.toml`, and updated operator docs (`AGENTS.md`, `ops/bmad/clean-worktree-automation.md`) to make preflight mandatory before mutating loop actions.

## Out of scope
- Automatic hygiene remediation actions (ticketing/routing automation remains manual policy).

## Verification evidence
- `mise run smoketest:bmad-preflight-strict-clean` → `PASS`
- `python3 -m py_compile ops/bmad/preflight_strict_clean.py` → success
- `mise run bmad:preflight-strict-clean` (on dirty working branch) → exit `1` with `blocking_reason: worktree_dirty`

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/160
- PR: <url>
- Follow-up tickets: none

## Notes
- Helper is read-only and intentionally fails fast when strict-clean gate is not met.
