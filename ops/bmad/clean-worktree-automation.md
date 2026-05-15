# Clean-worktree automation path (Hermes/BMAD)

Use this when your primary checkout is dirty and/or behind origin, and you need a safe ticket-first automation loop without touching local WIP.

## Guardrails

- Never stash, discard, or rewrite unknown local changes in the primary checkout.
- Run automation work in a dedicated clean worktree created from `origin/main`.
- Verify the automation worktree is clean and synced before creating ticket branches.
- Keep primary checkout untouched; remove the temporary worktree when done.

## Command recipe (copy/paste)

Preferred helper path:

```bash
# from primary checkout (may be dirty)
ISSUE_ID=<id> SLUG=<slug> mise run bmad:worktree-bootstrap
# optional: REUSE=1 ISSUE_ID=<id> SLUG=<slug> mise run bmad:worktree-bootstrap
```

Manual equivalent:

```bash
# from primary checkout (may be dirty)
git fetch origin main

# create isolated clean worktree rooted at origin/main
git worktree add -b fix/issue-<id>-<slug> /tmp/bloodbank-issue-<id> origin/main

# work from isolated tree
cd /tmp/bloodbank-issue-<id>
git status -sb  # expect clean branch off origin/main

# implement + verify
# ... edits/tests ...

git add <files>
git commit -m "<type>: <summary>"
git push -u origin fix/issue-<id>-<slug>

gh pr create --base main --head fix/issue-<id>-<slug> --title "<title>" --body "Closes #<id>"

# optional cleanup after merge
cd -
git worktree remove /tmp/bloodbank-issue-<id>
```

Optional safe merge helper (handles linked-worktree delete edge case):

```bash
mise run bmad:pr-merge-safe -- <pr-number-or-url>
```

## Notes

- If `/tmp/bloodbank-issue-<id>` already exists, remove it first or choose a different path.
- If your automation branch needs updates from `main`, pull/rebase in the isolated worktree only.
- Keep ticket evidence in workspace memory logs as usual.
