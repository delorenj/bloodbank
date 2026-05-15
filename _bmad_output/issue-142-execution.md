# Issue 142 — execution closeout

## Ticket
- Issue: #142
- Title: Prevent closeout evidence artifacts from dirtying primary checkout
- Owner: hermes (pilot)

## Scope completed
- Added ignore rule for runtime closeout artifacts: `_bmad_output/evidence/closeout/*.json`.
- Updated BMAD baseline docs (`AGENTS.md`) to mark closeout evidence JSONs as operator-generated and intentionally git-ignored.

## Out of scope
- Relocating evidence storage outside repository paths.

## Verification evidence
- `git status --short --branch` before fix showed untracked `_bmad_output/evidence/`.
- `git status --short --branch` after ignore update no longer shows `_bmad_output/evidence/` as untracked.

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/142
- PR: <url>
- Follow-up tickets: none

## Notes
- Existing tracked BMAD docs/templates remain unaffected.
