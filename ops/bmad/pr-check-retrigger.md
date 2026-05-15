# PR check retrigger (no-op commit alternative)

When a PR workflow run cannot be rerun from GitHub UI/CLI (for example queued jobs in a failed run), dispatch CI directly on the PR head branch:

```bash
mise run bmad:retrigger-pr-checks -- <pr-number>
```

Dry-run (contract + target verification only):

```bash
mise run bmad:retrigger-pr-checks -- <pr-number> --dry-run
```

This helper emits JSON with:
- target repo + branch,
- dispatch status (`dispatch_exit`, `dispatch_stderr`),
- follow-up commands to inspect check state.

Use this before falling back to manual empty-commit retriggers.
