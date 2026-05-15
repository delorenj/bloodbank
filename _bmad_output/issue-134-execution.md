# Issue 134 — execution closeout

## Ticket
- Issue: #134
- Title: Make merge_pr_safe cleanup follow-up idempotent for missing local branches
- Owner: hermes (pilot)

## Scope completed
- Updated `ops/bmad/merge_pr_safe.py` to make local branch cleanup idempotent when branch is already absent.
- Added explicit cleanup status field `cleanup.local_branch_status` with values `{already_absent, deleted, failed, not_applicable}`.
- Updated merge helper smoke contract to validate new status semantics.

## Out of scope
- Additional closeout-loop/reporting changes outside merge helper JSON contract.

## Verification evidence
- `mise run smoketest:bmad-merge-pr-safe` → `PASS`
- `python3 -m py_compile ops/bmad/merge_pr_safe.py` → success

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/134
- PR: <url>
- Follow-up tickets: none

## Notes
- This removes noisy follow-up `git branch -d ...` guidance when branch is already missing locally.
