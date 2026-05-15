# Issue 158 — execution closeout

## Ticket
- Issue: #158
- Title: Document full gh_readonly_status command surface (include repo-view)
- Owner: hermes (pilot)

## Scope completed
- Updated `AGENTS.md` task table + operator guidance to include full helper surface: `issue-view`, `pr-view`, and `repo-view`.
- Updated `mise.toml` task description for `bmad:gh-readonly-status` to reflect issue/pr/repo support.
- Updated `ops/bmad/github-cli-reliability.md` examples to include `repo-view`.

## Out of scope
- Runtime behavior changes to `gh_readonly_status.py`.

## Verification evidence
- `mise run bmad:gh-readonly-status -- repo-view` → `ok: true` with `nameWithOwner` payload.
- `rg -n "repo-view|issue/pr/repo" AGENTS.md mise.toml ops/bmad/github-cli-reliability.md` → updated command surface confirmed.

## Links
- Issue: https://github.com/delorenj/bloodbank/issues/158
- PR: <url>
- Follow-up tickets: none

## Notes
- This is doc/task-contract alignment only; no functional code-path change.
