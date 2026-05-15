#!/usr/bin/env python3
"""Cleanup helper for post-recovery backup branches and bundle artifacts.

Default mode is dry-run preview. Use --apply to execute deletion.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


BACKUP_PREFIX = "backup/main-divergence-"
BUNDLE_GLOB = "bloodbank-main-*.bundle"


def _run(repo: Path, *cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(repo), text=True, capture_output=True, check=False)


def _list_backup_branches(repo: Path) -> list[str]:
    cp = _run(repo, "git", "for-each-ref", "--format=%(refname:short)", f"refs/heads/{BACKUP_PREFIX}*")
    if cp.returncode != 0 or not cp.stdout.strip():
        return []
    return sorted([line.strip() for line in cp.stdout.splitlines() if line.strip()])


def _list_bundle_paths(bundle_dir: Path) -> list[Path]:
    if not bundle_dir.exists():
        return []
    return sorted(bundle_dir.glob(BUNDLE_GLOB))


def _plan_keep_remove(items: list[Any], keep: int) -> tuple[list[Any], list[Any]]:
    if keep <= 0:
        return [], items
    if len(items) <= keep:
        return items, []
    split = len(items) - keep
    return items[split:], items[:split]


def _delete_branch(repo: Path, branch: str) -> tuple[bool, str]:
    cp = _run(repo, "git", "branch", "-D", branch)
    if cp.returncode != 0:
        return False, cp.stderr.strip() or "branch delete failed"
    return True, "deleted"


def _delete_file(path: Path) -> tuple[bool, str]:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return False, str(exc)
    return True, "deleted"


def evaluate(repo: Path, bundle_dir: Path, keep_branches: int, keep_bundles: int, apply: bool) -> dict[str, Any]:
    branches = _list_backup_branches(repo)
    bundles = _list_bundle_paths(bundle_dir)

    keep_branch, remove_branch = _plan_keep_remove(branches, keep_branches)
    keep_bundle, remove_bundle = _plan_keep_remove(bundles, keep_bundles)

    payload: dict[str, Any] = {
        "ok": True,
        "repo": str(repo),
        "bundle_dir": str(bundle_dir),
        "dry_run": not apply,
        "keep_branches": keep_branches,
        "keep_bundles": keep_bundles,
        "backup_branches_total": len(branches),
        "bundle_files_total": len(bundles),
        "backup_branches_kept": keep_branch,
        "backup_branches_remove": remove_branch,
        "bundle_files_kept": [str(p) for p in keep_bundle],
        "bundle_files_remove": [str(p) for p in remove_bundle],
        "backup_branches_removed": [],
        "bundle_files_removed": [],
        "errors": [],
    }

    if not apply:
        return payload

    for br in remove_branch:
        ok, msg = _delete_branch(repo, br)
        if ok:
            payload["backup_branches_removed"].append(br)
        else:
            payload["ok"] = False
            payload["errors"].append(f"branch:{br}: {msg}")

    for fp in remove_bundle:
        ok, msg = _delete_file(fp)
        if ok:
            payload["bundle_files_removed"].append(str(fp))
        else:
            payload["ok"] = False
            payload["errors"].append(f"bundle:{fp}: {msg}")

    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Cleanup backup branches and bundle artifacts")
    parser.add_argument("--repo", default=".", help="Repo root (default: .)")
    parser.add_argument("--bundle-dir", default="/tmp", help="Bundle directory (default: /tmp)")
    parser.add_argument("--keep-branches", type=int, default=1, help="Keep latest N backup branches (default: 1)")
    parser.add_argument("--keep-bundles", type=int, default=1, help="Keep latest N bundle files (default: 1)")
    parser.add_argument("--apply", action="store_true", help="Execute deletion plan")
    args = parser.parse_args()

    keep_branches = max(0, args.keep_branches)
    keep_bundles = max(0, args.keep_bundles)

    payload = evaluate(
        Path(args.repo).resolve(),
        Path(args.bundle_dir).resolve(),
        keep_branches,
        keep_bundles,
        args.apply,
    )
    print(json.dumps(payload, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
