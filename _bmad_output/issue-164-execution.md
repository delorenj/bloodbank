# Issue 164 — execution closeout

## Ticket
- Issue: #164
- Title: Handle post-merge main divergence (ahead/behind equivalent patch) in operator loops
- Owner: hermes (pilot)

## Scope completed
- Added `ops/bmad/reconcile_main_divergence.py` to detect local `main...origin/main` divergence and classify patch-equivalent drift.
- Added optional `--apply` path (guarded) to reconcile only when on `main` and divergence is patch-equivalent.
- Added deterministic smoke test `ops/smoketest/smoketest-bmad-reconcile-main-divergence.sh` and documented/wired task surface in `mise.toml` and `AGENTS.md`.

## Out of scope
- Automatic invocation of reconcile helper from all loop entrypoints.

## Verification evidence
- `bash ops/smoketest/smoketest-bmad-reconcile-main-divergence.sh` → `PASS`
- `python3 -m py_compile ops/bmad/reconcile_main_divergence.py` → success
- `python3 ops/bmad/reconcile_main_divergence.py --repo /home/delorenj/code/33GOD/bloodbank` → `ahead: 1`, `behind: 1`, `patch_equivalent_divergence: true`, recommended reset action

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/164
- PR: <url>
- Follow-up tickets: none

## Notes
- Helper defaults to read-only; `--apply` is explicit and guarded.
