# Issue 162 — execution closeout

## Ticket
- Issue: #162
- Title: Enforce strict-clean preflight in mutating BMAD operator entrypoints
- Owner: hermes (pilot)

## Scope completed
- Enforced strict-clean preflight in mutating merge entrypoint `ops/bmad/merge_pr_safe.py` via `preflight_strict_clean.py` (default behavior).
- Added explicit override path `--bypass-preflight` with audit signal in JSON output (`preflight.bypassed = true`).
- Added deterministic guard smoke coverage `ops/smoketest/smoketest-bmad-merge-pr-preflight-guard.sh` (blocked path + bypass path), and updated existing merge smoke to run with explicit bypass.
- Updated task/docs wiring in `mise.toml` and `AGENTS.md`.

## Out of scope
- Enforcement wiring in additional mutating entrypoints beyond `merge_pr_safe.py`.

## Verification evidence
- `mise run smoketest:bmad-merge-pr-safe` → `PASS`
- `mise run smoketest:bmad-merge-pr-preflight-guard` → `PASS`
- `mise run smoketest:bmad-preflight-strict-clean` → `PASS`
- `python3 -m py_compile ops/bmad/merge_pr_safe.py ops/bmad/preflight_strict_clean.py` → success

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/162
- PR: <url>
- Follow-up tickets: none

## Notes
- Merge helper now fails fast with a machine-readable `BLOCKED` report when strict-clean preflight fails.
