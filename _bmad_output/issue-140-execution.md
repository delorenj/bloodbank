# Issue 140 — execution closeout

## Ticket
- Issue: #140
- Title: Persist closeout-loop artifacts so cleanup summary has live data
- Owner: hermes (pilot)

## Scope completed
- Added persistent artifact path support to `ops/bmad/closeout_loop.py` via `--out`.
- Added artifact wrapper task/script `bmad:closeout-loop:artifact` (`ops/bmad/closeout_loop_artifact.sh`) writing to `_bmad_output/evidence/closeout/`.
- Updated summary helper default directory to `_bmad_output/evidence/closeout`.
- Added artifact+summary smoke coverage (`smoketest-bmad-closeout-artifact-summary`) and task/docs wiring (`mise.toml`, `AGENTS.md`, `ops/bmad/closeout-cleanup-summary.md`).

## Out of scope
- Historical backfill of all prior closeout runs.

## Verification evidence
- `mise run smoketest:bmad-closeout-artifact-summary` → `PASS`
- `mise run bmad:closeout-loop:artifact -- 139` + `mise run bmad:closeout-cleanup-summary -- --limit 5` → summary now returns `count: 1` with live artifact item

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/140
- PR: <url>
- Follow-up tickets: none

## Notes
- Artifacted closeout run during validation is read-only from a merge perspective (PR already merged) but intentionally records current drift status snapshot.
