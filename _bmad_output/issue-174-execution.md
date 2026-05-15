# Issue 174 — execution closeout

## Ticket
- Issue: #174
- Title: Add backup-first helper to align diverged primary main safely
- Owner: hermes (pilot)

## Scope completed
- Added `ops/bmad/align_main_with_backup.py` backup-first alignment helper (read-only default, guarded `--apply`).
- Added deterministic smoke coverage `ops/smoketest/smoketest-bmad-align-main-with-backup.sh`.
- Wired task + docs surfaces in `mise.toml`, `AGENTS.md`, and `ops/bmad/primary-checkout-recovery.md`.

## Out of scope
- Automatic trigger/execution against primary checkout without explicit operator `--apply` invocation.

## Verification evidence
- `bash ops/smoketest/smoketest-bmad-align-main-with-backup.sh` → PASS
- `python3 -m py_compile ops/bmad/align_main_with_backup.py` → success
- `python3 ops/bmad/align_main_with_backup.py --repo /home/delorenj/code/33GOD/bloodbank --timestamp 20260515T221000Z` → read-only plan output, `recommended_action=backup_then_reset_origin_main`

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/174
- PR: <url>
- Follow-up tickets: none

## Notes
- Primary checkout drift currently reports `ahead 1, behind 6`; helper now provides deterministic backup/reset contract for safe canonical alignment.
