# Issue 156 — execution closeout

## Ticket
- Issue: #156
- Title: Harden Hermes runtime artifact hygiene (ignore + clean skeleton + secret-safe defaults)
- Owner: hermes (pilot)

## Scope completed
- Added/committed repo-local Hermes skeleton under `agents/` (docs + launcher/provision scripts + `runtime/.gitignore`) while keeping runtime secrets/session state excluded.
- Promoted runtime ignore rule in root `.gitignore` for `agents/hermes/runtime/**` with explicit allowlist for `runtime/.gitignore`.
- Added deterministic local smoke test `ops/smoketest/smoketest-hermes-runtime-hygiene.sh` and wired it into `mise.toml` + `AGENTS.md` task docs.

## Out of scope
- Rotation/migration of any existing local runtime secrets.

## Verification evidence
- `mise run smoketest:hermes-runtime-hygiene` → `PASS`
- `git status --short --ignored agents/hermes/runtime` → runtime secret/state files ignored (`!!`) under `agents/hermes/runtime`

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/156
- PR: <url>
- Follow-up tickets: none

## Notes
- `agents/hermes/runtime/` remains operator-local at runtime; only `.gitignore` is tracked there.
