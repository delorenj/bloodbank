#!/usr/bin/env python3
"""Detect and optionally reconcile patch-equivalent local main divergence.

Default mode is read-only. Use --apply to hard-reset local main to origin/main
only when divergence is patch-equivalent.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _run(repo: Path, *cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(repo), text=True, capture_output=True, check=False)


def _branch(repo: Path) -> str:
    cp = _run(repo, "git", "rev-parse", "--abbrev-ref", "HEAD")
    return cp.stdout.strip() if cp.returncode == 0 else ""


def _collect_commits(repo: Path, expr: str, limit: int) -> list[str]:
    cp = _run(repo, "git", "log", "--oneline", f"--max-count={limit}", expr)
    if cp.returncode != 0 or not cp.stdout.strip():
        return []
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def evaluate(repo: Path, limit: int = 10) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": False,
        "repo": str(repo),
        "branch": _branch(repo),
        "ahead": None,
        "behind": None,
        "patch_equivalent_divergence": False,
        "recommended_action": None,
        "applied": False,
        "ahead_commits": [],
        "behind_commits": [],
        "errors": [],
    }

    _run(repo, "git", "fetch", "origin", "main")

    counts = _run(repo, "git", "rev-list", "--left-right", "--count", "main...origin/main")
    if counts.returncode != 0:
        payload["errors"] = [counts.stderr.strip() or "rev-list failed"]
        return payload

    left, right = counts.stdout.strip().split()
    ahead = int(left)
    behind = int(right)
    payload["ahead"] = ahead
    payload["behind"] = behind

    cherry = _run(repo, "git", "log", "--left-right", "--cherry-pick", "--oneline", "main...origin/main")
    if cherry.returncode != 0:
        payload["errors"] = [cherry.stderr.strip() or "cherry-pick log failed"]
        return payload

    patch_equiv = ahead > 0 and behind > 0 and cherry.stdout.strip() == ""
    payload["patch_equivalent_divergence"] = patch_equiv

    payload["ahead_commits"] = _collect_commits(repo, "origin/main..main", limit)
    payload["behind_commits"] = _collect_commits(repo, "main..origin/main", limit)

    if ahead == 0 and behind == 0:
        payload["ok"] = True
        payload["recommended_action"] = "none"
        return payload

    if patch_equiv:
        payload["recommended_action"] = "git checkout main && git reset --hard origin/main"
    elif behind > 0 and ahead == 0:
        payload["recommended_action"] = "git checkout main && git pull --ff-only"
    else:
        payload["recommended_action"] = "manual_rebase_or_merge_review"

    payload["ok"] = True
    return payload


def apply_if_safe(repo: Path, payload: dict[str, object]) -> tuple[bool, str]:
    if payload.get("branch") != "main":
        return False, "apply requires current branch = main"
    if payload.get("patch_equivalent_divergence") is not True:
        return False, "apply allowed only for patch-equivalent divergence"

    cp = _run(repo, "git", "reset", "--hard", "origin/main")
    if cp.returncode != 0:
        return False, cp.stderr.strip() or "git reset --hard failed"
    return True, "applied"


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect/reconcile patch-equivalent main divergence")
    parser.add_argument("--repo", default=".", help="Repo root (default: .)")
    parser.add_argument("--apply", action="store_true", help="Apply safe reconciliation when eligible")
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Max commit summaries per divergence side in output (default: 10)",
    )
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    payload = evaluate(repo, limit=max(1, args.limit))

    if args.apply:
        ok, msg = apply_if_safe(repo, payload)
        payload["applied"] = ok
        if not ok:
            errs = payload.get("errors")
            if isinstance(errs, list):
                errs.append(msg)
            else:
                payload["errors"] = [msg]
            print(json.dumps(payload, indent=2))
            return 1

        payload = evaluate(repo, limit=max(1, args.limit))
        payload["applied"] = True

    print(json.dumps(payload, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
