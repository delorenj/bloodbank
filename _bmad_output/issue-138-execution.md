# Issue 138 — execution closeout

## Ticket
- Issue: #138
- Title: Add closeout artifact cleanup-status summary helper
- Owner: hermes (pilot)

## Scope completed
- Added read-only helper: `ops/bmad/closeout_cleanup_summary.py`.
- Added smoke test: `ops/smoketest/smoketest-bmad-closeout-cleanup-summary.sh`.
- Wired new tasks + docs updates in `mise.toml`, `AGENTS.md`, and `ops/bmad/closeout-cleanup-summary.md`.

## Out of scope
- Backfilling historical closeout artifacts into `_bmad_output/evidence`.

## Verification evidence
- `mise run smoketest:bmad-closeout-cleanup-summary` → `PASS`
- `mise run bmad:closeout-cleanup-summary -- --limit 5` → JSON output contract validated on live evidence dir (`count: 0` currently)

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/138
- PR: <url>
- Follow-up tickets: none

## Notes
- Helper supports both new top-level cleanup fields and legacy nested merge cleanup shape.
