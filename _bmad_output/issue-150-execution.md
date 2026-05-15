# Issue 150 — execution closeout

## Ticket
- Issue: #150
- Title: Adopt retry-aware gh readonly helper across BMAD automation paths
- Owner: hermes (pilot)

## Scope completed
- Updated `ops/bmad/merge_pr_safe.py` to use `ops/bmad/gh_readonly_status.py pr-view` for merged-state verification reads.
- Updated `ops/bmad/retrigger_pr_checks.py` to resolve `repo-view` and `pr-view` through `gh_readonly_status.py` instead of raw direct `gh ... view` calls.
- Extended `ops/bmad/gh_readonly_status.py` with `repo-view` support and `mergedAt` in `pr-view` payload.
- Added migration guidance and intentional raw-`gh` mutation exceptions in `ops/bmad/github-cli-reliability.md` and updated operator guidance in `AGENTS.md`.

## Out of scope
- Mutating command retry adoption (intentionally excluded).

## Verification evidence
- `mise run smoketest:bmad-merge-pr-safe` → `PASS`
- `mise run smoketest:bmad-retrigger-pr-checks` → `PASS`
- `mise run smoketest:bmad-gh-readonly-status` → `PASS`
- `python3 -m py_compile ops/bmad/merge_pr_safe.py ops/bmad/retrigger_pr_checks.py ops/bmad/gh_readonly_status.py` → success

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/150
- PR: <url>
- Follow-up tickets: none

## Notes
- Read-only `gh` adoption is now centralized for operator scripts that query issue/PR/repo metadata.
