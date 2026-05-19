# Issue 191 — execution closeout

## Ticket
- Issue: #191
- Title: BMAD: handle Hermes runtime submodule gitlink drift in pilot strict-clean loop
- Owner: hermes

## Scope completed
- Added `submodule_gitlink_drifts` diagnostics to `cli/bb.py repo-health` by parsing `git submodule status --recursive` (`+` marker) and enriching with recorded gitlink commit from `git ls-tree HEAD <path>`.
- Added text-output rendering for drift count/details so strict-clean logs explain exact submodule gitlink mismatch paths/commits.
- Extended deterministic smoke test `ops/smoketest/smoketest-bmad-repo-health-helper-availability.sh` to cover drift detection contract in both JSON and text output.

## Out of scope
- Automatic submodule reconcile/mutation behavior.

## Verification evidence
- `python3 -m py_compile cli/bb.py` → success.
- `bash ops/smoketest/smoketest-bmad-repo-health-helper-availability.sh` → `PASS`.
- `bash ops/smoketest/smoketest-hermes-runtime-hygiene.sh` → `PASS`.

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/191
- PR: pending
- Follow-up tickets: none

## Notes
- In this patch, submodule drift is surfaced as explicit diagnostics plus a warning entry in `errors` when present.
