# BMAD scaffold (Bloodbank)

This repository uses a lightweight BMAD baseline for ticket-first execution.

## Minimal flow
1. Start from an existing ticket/issue.
2. Create or update a short execution note under `_bmad_output/` scoped to that ticket.
3. Implement in a focused branch/PR tied to the ticket.
4. Record verification evidence in the PR body and ticket comments.

## In this scaffold
- `templates/ticket-execution.md` — starter template for ticket-scoped execution notes.
- `_bmad_output/` — place for generated ticket execution artifacts.

Keep this lightweight; use only what helps drive shipping and verification.
