# Issue 177 — execution closeout

## Ticket
- Issue: #177
- Title: Add lifecycle hygiene for backup branches and recovery bundles
- Owner: hermes (pilot)

## Scope completed
- Added helper `ops/bmad/recovery_artifact_cleanup.py` for backup-branch and bundle cleanup (dry-run default, guarded apply mode).
- Added deterministic smoke test `ops/smoketest/smoketest-bmad-recovery-artifact-cleanup.sh`.
- Wired task + docs updates across `mise.toml`, `AGENTS.md`, and `ops/bmad/primary-checkout-recovery.md`.

## Out of scope
- Automatic execution of cleanup apply mode in operator loop (remains explicit/manual guard).

## Verification evidence
- `bash ops/smoketest/smoketest-bmad-recovery-artifact-cleanup.sh` → PASS
- `python3 -m py_compile ops/bmad/recovery_artifact_cleanup.py` → success
- `python3 ops/bmad/recovery_artifact_cleanup.py --repo /home/delorenj/code/33GOD/bloodbank --bundle-dir /tmp --keep-branches 1 --keep-bundles 1` → dry-run JSON plan rendered

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/177
- PR: <url>
- Follow-up tickets: none

## Notes
- Current state keeps one active recovery branch + one bundle; helper now provides deterministic retirement path when retention window expires.
