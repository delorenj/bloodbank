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

## Out of scope
- Wiring cron automation to consume `idle_gate.py` and conditionally skip artifact writes (follow-up step).

## Verification evidence
- `python3 -m py_compile ops/repo-health/idle_gate.py` → success.
- `mise run smoketest:bmad-repo-health-idle-gate` → `smoketest-bmad-repo-health-idle-gate: PASS`.
- `rg -n "Idle-state throttle pattern for pilot loops" ops/bmad/github-cli-reliability.md` → guidance section present.

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/188
- PR: none
- Follow-up tickets: none

## Notes
- Current loop still performs full strict/artifact/cleanup every wake until runtime wiring is applied.
