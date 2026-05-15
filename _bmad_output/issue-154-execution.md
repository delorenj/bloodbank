# Issue 154 — execution closeout

## Ticket
- Issue: #154
- Title: Add explicit command-contract smoke coverage for gh_readonly_status issue/pr views
- Owner: hermes (pilot)

## Scope completed
- Extended `ops/smoketest/smoketest-bmad-gh-readonly-status.sh` to validate `issue-view` and `pr-view` command contracts through `gh_readonly_status.main()` with mocked `_run_once` responses.
- Added assertions for expected GH argv construction and required payload keys (`ok`, `command`, selected `data` fields) for both commands.

## Out of scope
- Live network integration checks against GitHub API.

## Verification evidence
- `mise run smoketest:bmad-gh-readonly-status` → `PASS`
- `python3 -m py_compile ops/bmad/gh_readonly_status.py` → success

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/154
- PR: <url>
- Follow-up tickets: none

## Notes
- Coverage remains deterministic and offline (stdlib-only mocking).
