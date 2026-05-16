# Issue 180 — execution closeout

## Ticket
- Issue: #180
- Title: Add age-based retention policy for recovery bundle cleanup
- Owner: hermes (pilot)

## Scope completed
- Extended `ops/bmad/recovery_artifact_cleanup.py` with `--min-bundle-age-hours` age gate for bundle deletion.
- Added JSON age metadata fields (`bundle_file_age_hours`, `bundle_files_skip_too_young`, `bundle_files_remove_age_eligible`).
- Updated deterministic smoke coverage and operator docs/task descriptions (`AGENTS.md`, `mise.toml`, `primary-checkout-recovery.md`).

## Out of scope
- Automated timed execution; cleanup apply remains explicit/manual.

## Verification evidence
- `bash ops/smoketest/smoketest-bmad-recovery-artifact-cleanup.sh` → PASS
- `python3 -m py_compile ops/bmad/recovery_artifact_cleanup.py` → success
- `python3 ops/bmad/recovery_artifact_cleanup.py --repo /home/delorenj/code/33GOD/bloodbank --bundle-dir /tmp --keep-branches 0 --keep-bundles 0 --min-bundle-age-hours 24` → dry-run output shows young bundle skipped by age gate

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/180
- PR: <url>
- Follow-up tickets: none

## Notes
- Current fallback bundle (~1.3h old) is correctly protected by a 24h gate in dry-run output.
