# Safe GitHub body authoring for BMAD automation

Avoid inline `--body "..."` when markdown may include backticks or shell-significant characters.

## Preferred patterns

### Use the safe wrapper (recommended)

```bash
cat > /tmp/issue-body.md <<'EOF'
## Summary
Contains `backticks`, $dollar signs, and $(subshell-like) text literally.
EOF

ops/bmad/gh_safe_create.sh issue \
  --title "<title>" \
  --label bug \
  --body-file /tmp/issue-body.md
```

### Create issue (direct gh)

```bash
cat > /tmp/issue-body.md <<'EOF'
## Summary
...
EOF

gh issue create \
  --title "<title>" \
  --body-file /tmp/issue-body.md
```

### Create PR

```bash
cat > /tmp/pr-body.md <<'EOF'
## Summary
...

Closes #<id>
EOF

ops/bmad/gh_safe_create.sh pr \
  --base main \
  --head fix/issue-<id>-<slug> \
  --title "<title>" \
  --body-file /tmp/pr-body.md
```

### Edit issue/PR body reliably

```bash
body="$(cat /tmp/body.md)"
gh api repos/<owner>/<repo>/issues/<id> -X PATCH -f body="$body"
gh api repos/<owner>/<repo>/pulls/<id> -X PATCH -f body="$body"
```

## Quick reproducible check

```bash
cat > /tmp/gh-body-safety-check.md <<'EOF'
## Safety check
Preserve literals: `code`, $HOME, $(echo hi), and "quotes".
EOF

ISSUE_URL=$(ops/bmad/gh_safe_create.sh issue --title "body-file safety check" --label documentation --body-file /tmp/gh-body-safety-check.md)
ISSUE_NUMBER=$(basename "$ISSUE_URL")
gh issue view "$ISSUE_NUMBER" --json body --jq .body
```

Expected: output includes the exact literal text above (no shell expansion or stripped snippets).

## Rule of thumb

If body text is longer than a short sentence, use `--body-file` (or REST patch from a file-backed variable).
Never use inline multi-line `--body "..."` in automation.
