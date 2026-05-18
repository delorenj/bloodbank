# Issue 188 — execution closeout

## Ticket
- Issue: #188
- Title: BMAD: add idle-state throttle to Hermes pilot repo-health loop
- Owner: hermes

## Scope completed
- Added `ops/repo-health/idle_gate.py`, a deterministic idle-state throttle decision helper for pilot loops.
- Added smoke coverage `ops/smoketest/smoketest-bmad-repo-health-idle-gate.sh` and wired new tasks in `mise.toml` (`repo-health:idle-gate`, `smoketest:bmad-repo-health-idle-gate`).
- Documented operator usage and decision contract in `_bmad_output/README.md`.
- Added BMAD operator guidance for idle-throttle loop handling in `ops/bmad/github-cli-reliability.md`.
- Added integrated pilot helper `ops/repo-health/pilot_step.sh` plus `mise run repo-health:pilot-step` to execute idle gate + strict check + conditional artifact/cleanup flow.
- Added ignore rule for idle-gate decision artifacts: `_bmad_output/evidence/repo-health-idle-decision-*.json`.

## Out of scope
- Auto-replacing all existing external cron invocations with `repo-health:pilot-step` (operator rollout step remains).

## Verification evidence
- `python3 -m py_compile ops/repo-health/idle_gate.py` → success.
- `mise run smoketest:bmad-repo-health-idle-gate` → `smoketest-bmad-repo-health-idle-gate: PASS`.
- `rg -n "Idle-state throttle pattern for pilot loops" ops/bmad/github-cli-reliability.md` → guidance section present.
- `mise run repo-health:pilot-step` (after clean commit) → decision artifact emitted and strict + conditional full-capture flow executes.

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/188
- PR: none
- Follow-up tickets: none

## Notes
- Current loop still performs full strict/artifact/cleanup every wake until runtime wiring is applied.
