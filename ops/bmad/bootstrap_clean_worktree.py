#!/usr/bin/env python3
"""Bootstrap an isolated clean worktree for ticket-first automation loops.

Env vars:
- ISSUE_ID: required positive integer.
- SLUG: required branch slug (letters/numbers/dash/underscore accepted; normalized to kebab-case).
- WORKTREE_BASE: optional base dir for temp worktrees (default: /tmp).
- WORKTREE_PREFIX: optional path prefix (default: bloodbank-issue).
- REUSE: truthy to reuse existing path/branch worktree when available.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, text=True, capture_output=True)


def _require_issue_id(raw: str | None) -> str:
    if raw is None or not raw.strip():
        raise ValueError("ISSUE_ID is required (example: ISSUE_ID=106)")
    candidate = raw.strip()
    if not re.fullmatch(r"[1-9]\d*", candidate):
        raise ValueError("ISSUE_ID must be a positive integer")
    return candidate


def _require_slug(raw: str | None) -> str:
    if raw is None or not raw.strip():
        raise ValueError("SLUG is required (example: SLUG=worktree-bootstrap-helper)")
    normalized = re.sub(r"[^a-z0-9_-]+", "-", raw.strip().lower()).strip("-_")
    normalized = normalized.replace("_", "-")
    normalized = re.sub(r"-+", "-", normalized)
    if not normalized:
        raise ValueError("SLUG normalized to empty value; provide a meaningful slug")
    return normalized


def _worktree_paths() -> set[str]:
    out = _run("git", "worktree", "list", "--porcelain").stdout
    paths: set[str] = set()
    for line in out.splitlines():
        if line.startswith("worktree "):
            paths.add(line.split(" ", 1)[1].strip())
    return paths


def main() -> int:
    try:
        issue_id = _require_issue_id(os.getenv("ISSUE_ID"))
        slug = _require_slug(os.getenv("SLUG"))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    reuse = _truthy(os.getenv("REUSE"))
    worktree_base = Path(os.getenv("WORKTREE_BASE", "/tmp"))
    prefix = os.getenv("WORKTREE_PREFIX", "bloodbank-issue").strip() or "bloodbank-issue"

    branch = f"fix/issue-{issue_id}-{slug}"
    worktree_path = (worktree_base / f"{prefix}-{issue_id}").resolve()

    # Always sync main ref before creating/reusing worktree.
    _run("git", "fetch", "origin", "main")

    known_paths = _worktree_paths()
    path_exists = worktree_path.exists()
    path_known = str(worktree_path) in known_paths

    created = False
    reused = False

    if path_exists or path_known:
        if not reuse:
            print(
                f"worktree path already exists: {worktree_path} (set REUSE=1 to reuse)",
                file=sys.stderr,
            )
            return 3
        if not path_known:
            print(
                f"path exists but is not a registered git worktree: {worktree_path}",
                file=sys.stderr,
            )
            return 3
        reused = True
    else:
        branch_exists = (
            subprocess.run(
                ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
                text=True,
                capture_output=True,
            ).returncode
            == 0
        )
        if branch_exists:
            _run("git", "worktree", "add", str(worktree_path), branch)
        else:
            _run("git", "worktree", "add", "-b", branch, str(worktree_path), "origin/main")
        created = True

    action = "created" if created else "reused"
    print(f"WORKTREE_{action.upper()}: {worktree_path}")
    print(f"BRANCH: {branch}")
    print("NEXT:")
    print(f"  cd {worktree_path}")
    print("  git status -sb")
    print("  # implement + verify, then commit/push/pr")
    print(f"CLEANUP:")
    print(f"  git worktree remove {worktree_path}")
    if not reused:
        print(f"  git branch -d {branch}  # after merge")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
