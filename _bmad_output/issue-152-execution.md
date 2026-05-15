# Issue 152 — execution closeout

## Ticket
- Issue: #152
- Title: Add explicit smoke coverage for gh_readonly_status repo-view contract
- Owner: hermes (pilot)

## Scope completed
- Extended `ops/smoketest/smoketest-bmad-gh-readonly-status.sh` to explicitly validate the `repo-view` command contract in `ops/bmad/gh_readonly_status.py` using stdlib-only mocking.
- Added assertions for invoked GH argv (`gh repo view --json nameWithOwner`) and output payload contract (`ok=true`, `command=repo-view`, `data.nameWithOwner` present).

## Out of scope
- Live network integration tests against GitHub API.

## Verification evidence
- `mise run smoketest:bmad-gh-readonly-status` → `PASS`
- `python3 -m py_compile ops/bmad/gh_readonly_status.py` → success

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/152
- PR: <url>
- Follow-up tickets: none

## Notes
- Test remains deterministic and offline; no change to production helper behavior.
