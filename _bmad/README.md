# BMAD scaffold (Bloodbank)

This repository uses a lightweight BMAD baseline for ticket-first execution.

## Minimal flow
1. Start from an existing ticket/issue.
2. Draft scope with `templates/ticket-execution.md` (or update an existing ticket note).
3. Implement in a focused branch/PR tied to that ticket.
4. On completion, add/update `_bmad_output/issue-<id>-execution.md` as the closeout artifact.
5. Ensure closeout evidence quality and index entry follow `_bmad_output/README.md`.

## In this scaffold
- `templates/ticket-execution.md` — starter template for ticket-scoped execution notes.
- `_bmad_output/` — ticket closeout artifacts (`issue-<id>-execution.md`).
- `_bmad_output/README.md` — source of truth for closeout index + verification checklist expectations.

Keep this lightweight; use only what helps drive shipping and verification.
