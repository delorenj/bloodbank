# Issue 144 — execution closeout

## Ticket
- Issue: #144
- Title: Add bounded retry helper for transient gh API connectivity errors
- Owner: hermes (pilot)

## Scope completed
- Added bounded retry helper in `cli/bb.py` for read-only GitHub status calls used by `repo-health`.
- Wired retries into `gh issue list`, `gh pr list`, and `gh pr checks` paths.
- Updated BMAD reliability docs (`ops/bmad/github-cli-reliability.md`) and `AGENTS.md` guidance.

## Out of scope
- Mutating GitHub operations (`gh pr merge`, `gh issue create`, etc.) retry policy.

## Verification evidence
- `python3 -m py_compile cli/bb.py` → success
- `mise run repo-health:artifact` → `_bmad_output/evidence/repo-health-20260515T132146Z.json`

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/144
- PR: <url>
- Follow-up tickets: none

## Notes
- Retry behavior is bounded (3 attempts with short backoff) and only applied when stderr matches transient connectivity signatures.
