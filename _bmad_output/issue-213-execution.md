# Issue 213 — execution log

## Ticket
- Issue: #213
- Title: BMAD: reconcile blocked by tracked runtime state.db drift

## Problem reproduced
- `bmad:reconcile-submodule-drift --apply` failed when runtime submodule was at drifted commit `2b1061b`.
- Blocking stderr included: `state.db would be overwritten by checkout`.

## Forward step implemented
- Hardened `ops/bmad/reconcile_submodule_gitlink_drift.py`:
  - On known runtime path `agents/hermes/pm/runtime`, if checkout fails with the `state.db` overwrite pattern, retry with `git checkout --detach -f <recorded>`.
  - Keeps strict behavior for all other paths/errors.
- Added smoketest coverage in `ops/smoketest/smoketest-bmad-reconcile-submodule-drift.sh` for this forced-checkout retry path.
- Updated AGENTS operator guidance to document the runtime `state.db` fallback behavior.

## Verification
- `bash ops/smoketest/smoketest-bmad-reconcile-submodule-drift.sh` => PASS

## Next
- Open PR, run checks, merge.
- Re-run `mise run bmad:reconcile-submodule-drift -- --repo /home/delorenj/code/33GOD/bloodbank --apply` in primary checkout to confirm blocker is cleared.
