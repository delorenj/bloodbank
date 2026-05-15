# Issue 136 — execution closeout

## Ticket
- Issue: #136
- Title: Expose normalized cleanup status in closeout_loop output
- Owner: hermes (pilot)

## Scope completed
- Added top-level normalized cleanup fields to `ops/bmad/closeout_loop.py`:
  - `cleanup_local_branch_status`
  - `cleanup_local_branch_deleted`
- Kept backward-compatible `cleanup_followup_commands` + warning behavior.
- Extended closeout-loop smoke assertions for normalized cleanup status values.

## Out of scope
- Additional dashboard/report consumers outside closeout-loop output contract.

## Verification evidence
- `mise run smoketest:bmad-closeout-loop` → `PASS`
- `python3 -m py_compile ops/bmad/closeout_loop.py` → success

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/136
- PR: <url>
- Follow-up tickets: none

## Notes
- Defaults to `cleanup_local_branch_status = "unknown"` when merge payload has no cleanup block.
