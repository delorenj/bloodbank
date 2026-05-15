# Issue 168 — execution closeout

## Ticket
- Issue: #168
- Title: Execute post-merge main reconciliation helper on primary checkout
- Owner: hermes (pilot)

## Scope completed
- Extended `ops/bmad/reconcile_main_divergence.py` with divergence side summaries (`ahead_commits`, `behind_commits`) and `--limit` control.
- Updated deterministic smoke test `ops/smoketest/smoketest-bmad-reconcile-main-divergence.sh` to validate commit-side summaries.
- Updated AGENTS task contract to include `--limit` and summarize new helper output intent.

## Out of scope
- Automatic repair for non patch-equivalent divergence (`manual_rebase_or_merge_review` remains explicit recommendation).

## Verification evidence
- `bash ops/smoketest/smoketest-bmad-reconcile-main-divergence.sh` → `PASS`
- `python3 -m py_compile ops/bmad/reconcile_main_divergence.py` → success
- `python3 ops/bmad/reconcile_main_divergence.py --repo /home/delorenj/code/33GOD/bloodbank --limit 5` → reports `ahead_commits`/`behind_commits` with manual reconciliation recommendation

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/168
- PR: <url>
- Follow-up tickets: none

## Notes
- This gives immediate operator evidence for the non-patch-equivalent blocker without mutating primary checkout.
