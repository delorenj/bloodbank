# Issue 189 — execution closeout

## Ticket
- Issue: #189
- Title: BMAD: unblock pilot strict-clean gate from Hermes runtime submodule artifacts
- Owner: hermes

## Scope completed
- Set `ignore = untracked` for `agents/hermes/pm/runtime` in `.gitmodules` so expected runtime scratch does not dirty parent checkout.
- Extended `ops/smoketest/smoketest-hermes-runtime-hygiene.sh` to assert the `pm/runtime` submodule ignore contract.

## Out of scope
- Any changes inside the `agents/hermes/pm/runtime` submodule repository itself.

## Verification evidence
- `mise run smoketest:hermes-runtime-hygiene` → `PASS`.
- `git status --short --branch` (before staging) → no lingering `? agents/hermes/pm/runtime` dirty marker from runtime scratch.

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/189
- PR: pending
- Follow-up tickets: none

## Notes
- Remaining parent checkout dirtiness is only from intentional tracked edits for this ticket.
