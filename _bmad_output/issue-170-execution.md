# Issue 170 — execution closeout

## Ticket
- Issue: #170
- Title: Recover helper availability when primary checkout diverges from merged BMAD updates
- Owner: hermes (pilot)

## Scope completed
- Added read-only diagnostics helper: `ops/bmad/primary_recovery_check.py`.
- Added operator runbook for non-destructive recovery sequencing: `ops/bmad/primary-checkout-recovery.md`.
- Registered task + smoke coverage in `mise.toml` and operator guide entries in `AGENTS.md`.

## Out of scope
- Auto-mutating recovery for non patch-equivalent divergence (remains manual decision path).

## Verification evidence
- `bash ops/smoketest/smoketest-bmad-primary-recovery-check.sh` → PASS
- `python3 -m py_compile ops/bmad/primary_recovery_check.py` → success
- `python3 ops/bmad/primary_recovery_check.py --repo /home/delorenj/code/33GOD/bloodbank` → reports `helper_local_exists=false`, `helper_on_origin_main=true`, `recommended_path=manual_rebase_or_backup_then_reset`

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/170
- PR: <url>
- Follow-up tickets: none

## Notes
- Primary checkout drift worsened from behind 3 to behind 4 during loop window due additional upstream merges.
