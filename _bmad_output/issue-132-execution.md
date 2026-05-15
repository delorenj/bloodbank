# Issue 132 — execution closeout

## Ticket
- Issue: #132
- Title: Harden BMAD GitHub body composition against shell interpolation hazards
- Owner: hermes (pilot)

## Scope completed
- Added shell-safe runbook `ops/bmad/github-body-authoring.md` with `--body-file` and file-backed REST patch patterns.
- Updated `ops/bmad/clean-worktree-automation.md` to use `gh pr create --body-file` instead of inline `--body`.
- Added guardrail smoke test `ops/smoketest/smoketest-bmad-github-body-safety.sh` and wired it into `mise` + `AGENTS.md` task docs.

## Out of scope
- Full historical rewrite of all non-BMAD docs outside `ops/bmad`.

## Verification evidence
- `mise run smoketest:bmad-github-body-safety` → `PASS`
- `rg -n "gh (issue create|pr create|pr edit|issue edit).*(--body(=| )\")" ops/bmad` → no matches

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/132
- PR: <url>
- Follow-up tickets: none

## Notes
- This specifically hardens BMAD operator paths where cron automation composes markdown-heavy bodies.
