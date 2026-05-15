# Issue 130 — execution closeout

## Ticket
- Issue: #130
- Title: Harden operator scripts against gh GraphQL projectCards deprecation failures
- Owner: hermes (pilot)

## Scope completed
- Added fallback runbook: `ops/bmad/github-cli-reliability.md`.
- Updated `AGENTS.md` BMAD baseline to require `--json`/`gh api` fallback patterns for GraphQL deprecation failures.

## Out of scope
- Refactoring every existing operator helper to REST-only paths.

## Verification evidence
- `gh issue view 130 --json state,title,url` → issue open and reachable via JSON mode.
- `mise run repo-health:artifact` → `_bmad_output/evidence/repo-health-20260515T094119Z.json`.

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/130
- PR: <url>
- Follow-up tickets: none

## Notes
- This ticket closes immediate loop reliability guidance; broader helper rewrites can be handled incrementally if deprecation failures recur.
