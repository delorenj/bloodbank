# Issue 38 — execution closeout

## Ticket
- Issue: #38
- Title: Document BMAD closeout artifact standard in _bmad_output
- Owner: hermes (pilot loop)

## Scope completed
- Documented required closeout fields in `_bmad_output/README.md`.
- Added starter template `_bmad_output/templates/ticket-closeout.md`.

## Out of scope
- Backfilling closeout artifacts for older non-BMAD tickets.

## Verification evidence
- `test -f _bmad_output/templates/ticket-closeout.md` → `template:present`
- `gh issue view 38 --json state --jq '.state'` → `CLOSED`
- `gh pr view 39 --json state,mergedAt --jq '"state=" + .state + ", mergedAt=" + .mergedAt'` → `state=MERGED, mergedAt=2026-05-14T08:40:32Z`

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/38
- PR: https://github.com/delorenj/bloodbank/pull/39
- Follow-up tickets: #40

## Notes
- This artifact is the first concrete closeout example requested by #40.
