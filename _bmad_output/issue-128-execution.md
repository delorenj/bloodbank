# Issue 128 — execution closeout

## Ticket
- Issue: #128
- Title: Investigate queued-workflow rerun failure for PR smoke jobs
- Owner: hermes (pilot)

## Scope completed
- Added `ops/bmad/retrigger_pr_checks.py` helper to dispatch CI workflow for a PR head branch without no-op commits.
- Added local dry-run contract smoke test and wired new mise tasks/documentation for operator recovery flow.

## Out of scope
- Root-cause proof for the queued/failure mismatch in GitHub Actions internals.

## Verification evidence
- `mise run smoketest:bmad-retrigger-pr-checks` → `PASS`
- `python3 -m py_compile ops/bmad/retrigger_pr_checks.py` → success (no syntax errors)

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/128
- PR: <url>
- Follow-up tickets: none

## Notes
- `mise run smoketest:ops` currently fails on dirty working tree because `smoketest:repo-health-cleanup` asserts strict-clean mode; expected during in-progress branch edits.
