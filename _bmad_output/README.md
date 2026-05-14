# BMAD output

Ticket-scoped execution artifacts live here (notes, checklists, evidence snippets).

## Closeout artifact convention

For every completed ticket, add one closeout file:
- Path: `_bmad_output/issue-<id>-execution.md`
- Starter template: `_bmad_output/templates/ticket-closeout.md`

Required fields:
- Ticket id + title
- Scope completed (what changed / what did not)
- Verification evidence (commands/checks and outcomes)
- Links to issue + PR (and follow-ups if any)

## Closeout artifact index

- `issue-38-execution.md` — closes out #38 via PR #39 (BMAD closeout standard)

Index append convention (for each new closeout file):
- `issue-<id>-execution.md` — closes out #<id> via PR #<pr> (<short scope note>)

## Verification checklist (closeout quality gate)

Before marking a ticket closed, confirm the closeout artifact includes:
- At least one concrete command/check with an explicit outcome (pass/fail or observed state).
- A direct state assertion for the ticket/PR pair (for example: issue closed, PR merged).
- Links to the exact issue and PR URLs (not just numbers).
- Any out-of-scope/deferred work called out explicitly (`none` when empty).
- Notes section completed with risks/caveats or `none`.

## Structured snapshot option (automation-friendly)

Use JSON mode when evidence needs to be consumed by scripts/tools (artifact generators, dashboards, checks):
- `python3 cli/bb.py repo-health --json`
