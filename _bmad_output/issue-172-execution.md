# Issue 172 — execution closeout

## Ticket
- Issue: #172
- Title: Expose BMAD helper availability in repo-health snapshot
- Owner: hermes (pilot)

## Scope completed
- Extended `cli/bb.py repo-health` snapshot with helper diagnostics:
  - `helper_local_exists`
  - `helper_on_origin_main`
- Added deterministic smoke test `ops/smoketest/smoketest-bmad-repo-health-helper-availability.sh`.
- Wired task docs/runner surface in `mise.toml` and `AGENTS.md`.

## Out of scope
- Auto-recovery/mutation of primary checkout divergence (still ticketed/manual path).

## Verification evidence
- `bash ops/smoketest/smoketest-bmad-repo-health-helper-availability.sh` → PASS
- `python3 -m py_compile cli/bb.py` → success
- `python3 cli/bb.py repo-health --json --limit 1` → includes helper availability fields in output

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/172
- PR: <url>
- Follow-up tickets: none

## Notes
- Improves loop routing when primary checkout is stale and merged helper paths are missing locally.
