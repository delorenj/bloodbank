# GitHub CLI reliability fallback (projectCards deprecation)

Some `gh` command paths may fail with GraphQL classic-projects deprecation errors, e.g.:

- `repository.issue.projectCards`
- `repository.pullRequest.projectCards`

Use these automation-safe patterns in BMAD loops.

## Preferred command patterns

### Issue/PR reads

- Prefer explicit JSON field queries:

```bash
gh issue view <id> --json number,title,state,url,body
gh pr view <id> --json number,title,state,url,headRefName,mergeStateStatus,statusCheckRollup
```

- If JSON path still fails, use REST directly:

```bash
gh api repos/<owner>/<repo>/issues/<id>
gh api repos/<owner>/<repo>/pulls/<id>
```

### Issue/PR edits

- Prefer REST patch for title/body edits (avoids GraphQL mutation path):

```bash
body="$(cat /tmp/body.md)"
gh api repos/<owner>/<repo>/issues/<id> -X PATCH -f title='...' -f body="$body"
gh api repos/<owner>/<repo>/pulls/<id> -X PATCH -f title='...' -f body="$body"
```

## Transient connectivity guard

For read-only repo snapshots, `cli/bb.py repo-health` now applies bounded retry (3 attempts, short backoff) for transient `gh` connectivity failures (e.g. `error connecting to api.github.com`) on:

- `gh issue list`
- `gh pr list`
- `gh pr checks`

Use `mise run repo-health` / `mise run repo-health:artifact` as the preferred status path in automation loops.

For direct issue/PR/repo status reads in automation loops, use the retry-aware helper:

```bash
mise run bmad:gh-readonly-status -- issue-view <id>
mise run bmad:gh-readonly-status -- pr-view <id>
mise run bmad:gh-readonly-status -- repo-view
```

Local regression checks:

```bash
mise run smoketest:bmad-repo-health-retry
mise run smoketest:bmad-gh-readonly-status
mise run smoketest:bmad-repo-health-idle-gate
```

## Idle-state throttle pattern for pilot loops

For stable loops, evaluate the idle gate before writing full evidence snapshots:

```bash
python3 cli/bb.py repo-health --json --out /tmp/repo-health.json
python3 ops/repo-health/idle_gate.py --snapshot /tmp/repo-health.json --interval-minutes 60
```

Decision handling:

- `should_capture_full=true`: run strict gate + artifact + cleanup.
- `should_capture_full=false`: skip full artifact rotation for this wake.
- Any non-idle repo state should force `should_capture_full=true`.

## Intentional raw-`gh` exceptions (currently)

The retry helper is intentionally **not** used for mutating operations:

- `gh pr merge ...`
- `gh issue create ...`
- `gh pr create ...`
- `gh api -X POST ...` dispatch/mutation calls

Those paths should surface failures immediately to avoid accidental duplicate writes.

## Operational checklist

1. Capture failing `gh` command + stderr in the ticket evidence.
2. Retry using `--json` field-limited read.
3. Fall back to `gh api` REST for reads/edits.
4. Keep loop moving; do not block BMAD closeout on GraphQL-only paths.
