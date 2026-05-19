# Issue 195 — execution closeout

## Ticket
- Issue: #195
- Title: BMAD: add safe reconcile path for Hermes PM submodule gitlink drift
- Owner: hermes

## Scope completed
- Added `ops/bmad/reconcile_submodule_gitlink_drift.py` (read-only by default, optional `--apply`) to detect and reconcile submodule gitlink drift back to superproject-recorded commits.
- Added mise task `bmad:reconcile-submodule-drift`.
- Added smoketest `ops/smoketest/smoketest-bmad-reconcile-submodule-drift.sh` and wired task `smoketest:bmad-reconcile-submodule-drift`.
- Extended `smoketest:ops` chain to include new drift-reconcile smoketest.
- Updated `AGENTS.md` task and BMAD guidance references for the new helper workflow.

## Out of scope
- Automatic invocation of reconcile helper from pilot-step.

## Verification evidence
- `python3 -m py_compile cli/bb.py ops/bmad/reconcile_submodule_gitlink_drift.py` → success.
- `bash ops/smoketest/smoketest-bmad-repo-health-helper-availability.sh` → `PASS`.
- `bash ops/smoketest/smoketest-bmad-reconcile-submodule-drift.sh` → `PASS`.
- `bash ops/smoketest/smoketest-hermes-runtime-hygiene.sh` → `PASS`.
- `python3 ops/bmad/reconcile_submodule_gitlink_drift.py --repo /home/delorenj/code/33GOD/bloodbank` → reports current runtime drift with clean diagnostics.

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/195
- PR: pending
- Follow-up tickets: none

## Notes
- Helper apply mode requires `main` branch and refuses when non-drift paths are dirty.
