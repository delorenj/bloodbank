#!/usr/bin/env python3
"""Read-only diagnostics for recovering a diverged primary checkout."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


def _run(repo: Path, *cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, check=False)


def _exists_on_ref(repo: Path, ref_path: str) -> bool:
    cp = _run(repo, "git", "cat-file", "-e", ref_path)
    return cp.returncode == 0


def evaluate(repo: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "repo": str(repo),
        "branch": "",
        "git_status": "",
        "ahead": None,
        "behind": None,
        "patch_equivalent_divergence": False,
        "helper_local_exists": False,
        "helper_on_origin_main": False,
        "recommended_path": "",
        "errors": [],
    }

    status = _run(repo, "git", "status", "--short", "--branch")
    if status.returncode != 0:
        payload["errors"].append(status.stderr.strip() or "git status failed")
        return payload

    lines = status.stdout.splitlines()
    payload["git_status"] = lines[0] if lines else ""

    branch = _run(repo, "git", "rev-parse", "--abbrev-ref", "HEAD")
    payload["branch"] = branch.stdout.strip() if branch.returncode == 0 else ""

    _run(repo, "git", "fetch", "origin", "main")

    counts = _run(repo, "git", "rev-list", "--left-right", "--count", "main...origin/main")
    if counts.returncode != 0:
        payload["errors"].append(counts.stderr.strip() or "rev-list failed")
        return payload

    left, right = counts.stdout.strip().split()
    ahead, behind = int(left), int(right)
    payload["ahead"] = ahead
    payload["behind"] = behind

    cherry = _run(repo, "git", "log", "--left-right", "--cherry-pick", "--oneline", "main...origin/main")
    if cherry.returncode != 0:
        payload["errors"].append(cherry.stderr.strip() or "cherry log failed")
        return payload

    patch_equiv = ahead > 0 and behind > 0 and cherry.stdout.strip() == ""
    payload["patch_equivalent_divergence"] = patch_equiv

    helper_rel = Path("ops/bmad/reconcile_main_divergence.py")
    payload["helper_local_exists"] = (repo / helper_rel).exists()
    payload["helper_on_origin_main"] = _exists_on_ref(repo, f"origin/main:{helper_rel.as_posix()}")

    if ahead == 0 and behind == 0:
        payload["recommended_path"] = "none"
    elif patch_equiv and payload["helper_local_exists"]:
        payload["recommended_path"] = "run_reconcile_helper_apply"
    elif patch_equiv and not payload["helper_local_exists"] and payload["helper_on_origin_main"]:
        payload["recommended_path"] = "align_main_then_run_reconcile_helper"
    elif ahead > 0 and behind > 0:
        payload["recommended_path"] = "manual_rebase_or_backup_then_reset"
    elif behind > 0:
        payload["recommended_path"] = "pull_ff_only"
    else:
        payload["recommended_path"] = "review_local_ahead_commits"

    payload["ok"] = True
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Primary checkout divergence diagnostics")
    parser.add_argument("--repo", default=".", help="Repo root (default: .)")
    args = parser.parse_args()

    payload = evaluate(Path(args.repo).resolve())
    print(json.dumps(payload, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
