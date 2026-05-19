# Issue 193 — execution closeout

## Ticket
- Issue: #193
- Title: BMAD: fix repo-health exit-code regression from submodule drift warnings
- Owner: hermes

## Scope completed
- Changed `cli/bb.py repo-health` to classify submodule drift notices as `warnings` instead of `errors`.
- Preserved drift diagnostics (`submodule_gitlink_drifts`) in JSON and text output while keeping exit code reserved for hard failures.
- Updated deterministic smoke coverage in `ops/smoketest/smoketest-bmad-repo-health-helper-availability.sh` to enforce the non-failing warning contract.

## Out of scope
- Automatic submodule drift reconciliation/mutation logic.

## Verification evidence
- `python3 -m py_compile cli/bb.py` → success.
- `bash ops/smoketest/smoketest-bmad-repo-health-helper-availability.sh` → `PASS`.
- `bash ops/smoketest/smoketest-hermes-runtime-hygiene.sh` → `PASS`.

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/193
- PR: pending
- Follow-up tickets: none

## Notes
- This restores `repo-health:pilot-step` behavior where strict-clean blocking is governed by the strict gate, not by drift diagnostics capture.
