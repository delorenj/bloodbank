# Issue 197 — execution closeout

## Ticket
- Issue: #197
- Title: BMAD: add pilot-step drift auto-heal for Hermes PM submodule checkpoint churn
- Owner: hermes

## Scope completed
- Updated `ops/repo-health/pilot_step.sh` with one-shot strict-fail auto-heal flow:
  - run strict gate
  - on strict failure, run `ops/bmad/reconcile_submodule_gitlink_drift.py --apply`
  - retry strict gate once
- Added `DRIFT_AUTOHEAL_ON_STRICT_FAIL` env toggle (default `1`; set `0` to disable).
- Added deterministic smoke coverage `ops/smoketest/smoketest-bmad-repo-health-pilot-step-autoheal.sh`.
- Wired new smoketest into `mise.toml` (`smoketest:bmad-repo-health-pilot-step-autoheal`) and `smoketest:ops` chain.
- Updated `AGENTS.md` task table + guidance for pilot-step auto-heal behavior.

## Out of scope
- Automatic ticket creation/escalation for repeated drift events.

## Verification evidence
- `bash ops/smoketest/smoketest-bmad-repo-health-idle-gate.sh` → PASS.
- `bash ops/smoketest/smoketest-bmad-repo-health-pilot-step-autoheal.sh` → PASS.
- `bash ops/smoketest/smoketest-bmad-reconcile-submodule-drift.sh` → PASS.

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/197
- PR: pending
- Follow-up tickets: none

## Notes
- Auto-heal remains guardrailed by reconcile helper safety checks (main branch + no non-drift dirty paths).
