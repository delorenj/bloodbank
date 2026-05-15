# Issue 148 — execution closeout

## Ticket
- Issue: #148
- Title: Extend bounded gh retry coverage beyond repo-health status paths
- Owner: hermes (pilot)

## Scope completed
- Added read-only status helper `ops/bmad/gh_readonly_status.py` with bounded retry for transient `gh` API connectivity failures.
- Helper supports JSON-first status calls for `issue-view` and `pr-view`.
- Added deterministic smoke test `ops/smoketest/smoketest-bmad-gh-readonly-status.sh` and task/docs wiring in `mise.toml`, `AGENTS.md`, and `ops/bmad/github-cli-reliability.md`.

## Out of scope
- Retry policy expansion to mutating GitHub operations.

## Verification evidence
- `mise run smoketest:bmad-gh-readonly-status` → `PASS`
- `mise run bmad:gh-readonly-status -- issue-view 148` → `ok: true`, `attempts: 1`, JSON payload returned

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/148
- PR: <url>
- Follow-up tickets: none

## Notes
- Retry behavior mirrors repo-health policy (3 attempts, short backoff, transient-signature gated).
