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

Local regression check:

```bash
mise run smoketest:bmad-repo-health-retry
```

## Operational checklist

1. Capture failing `gh` command + stderr in the ticket evidence.
2. Retry using `--json` field-limited read.
3. Fall back to `gh api` REST for reads/edits.
4. Keep loop moving; do not block BMAD closeout on GraphQL-only paths.
