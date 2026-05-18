# Issue 187 — execution closeout

## Ticket
- Issue: #187
- Title: harden GitHub issue authoring against shell interpolation
- Owner: hermes

## Scope completed
- Added `ops/bmad/gh_safe_create.sh`, a wrapper enforcing `--body-file` for `gh issue create` and `gh pr create`.
- Updated `ops/bmad/github-body-authoring.md` to prefer wrapper usage and added a reproducible markdown-literal preservation check.
- Scaffolded issue execution artifact for ticket-first traceability.

## Out of scope
- Closing issue #187 in this run (follow-up run will complete/verify end-to-end usage in active workflows).

## Verification evidence
- `ops/bmad/gh_safe_create.sh --help` → usage printed with required `--body-file` guidance.
- `ops/bmad/gh_safe_create.sh issue --title test --body "bad"` → exits 2 with inline-body rejection error.
- `bash ops/smoketest/smoketest-bmad-github-body-safety.sh` → `PASS`.

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/187
- PR: none
- Follow-up tickets: none

## Notes
- This step hardens operator ergonomics and prevents zsh interpolation damage when authoring markdown bodies in automation loops.
