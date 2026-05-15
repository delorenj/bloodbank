# Safe GitHub body authoring for BMAD automation

Avoid inline `--body "..."` when markdown may include backticks or shell-significant characters.

## Preferred patterns

### Create issue

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

gh pr create \
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

## Rule of thumb

If body text is longer than a short sentence, use `--body-file` (or REST patch from a file-backed variable).
