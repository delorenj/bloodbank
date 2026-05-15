#!/usr/bin/env python3
"""Backup-first alignment helper for diverged local main vs origin/main.

Default mode is read-only planning output. Use --apply to create a backup branch,
write a bundle artifact, then hard-reset local main to origin/main.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _run(repo: Path, *cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(repo), text=True, capture_output=True, check=False)


def _branch(repo: Path) -> str:
    cp = _run(repo, "git", "rev-parse", "--abbrev-ref", "HEAD")
    return cp.stdout.strip() if cp.returncode == 0 else ""


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def evaluate(repo: Path, bundle_dir: Path, timestamp: str | None = None) -> dict[str, Any]:
    ts = timestamp or _now_ts()
    backup_branch = f"backup/main-divergence-{ts}"
    bundle_path = bundle_dir / f"bloodbank-main-{ts}.bundle"

    payload: dict[str, Any] = {
        "ok": False,
        "repo": str(repo),
        "branch": _branch(repo),
        "ahead": None,
        "behind": None,
        "patch_equivalent_divergence": False,
        "backup_branch": backup_branch,
        "bundle_path": str(bundle_path),
        "backup_created": False,
        "bundle_created": False,
        "reset_applied": False,
        "recommended_action": None,
        "commands": [
            f"git branch {backup_branch} main",
            f"git bundle create {bundle_path} main {backup_branch}",
            "git checkout main",
            "git fetch origin main",
            "git reset --hard origin/main",
        ],
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

    payload["patch_equivalent_divergence"] = ahead > 0 and behind > 0 and cherry.stdout.strip() == ""

    if ahead == 0 and behind == 0:
        payload["ok"] = True
        payload["recommended_action"] = "none"
        return payload

    payload["ok"] = True
    payload["recommended_action"] = "backup_then_reset_origin_main"
    return payload


def apply(repo: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    if payload.get("branch") != "main":
        return False, "apply requires current branch = main"

    if payload.get("ahead") == 0 and payload.get("behind") == 0:
        return True, "already_aligned"

    backup_branch = str(payload["backup_branch"])
    bundle_path = Path(str(payload["bundle_path"]))

    cp = _run(repo, "git", "branch", backup_branch, "main")
    if cp.returncode != 0:
        return False, cp.stderr.strip() or "backup branch create failed"
    payload["backup_created"] = True

    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    cp = _run(repo, "git", "bundle", "create", str(bundle_path), "main", backup_branch)
    if cp.returncode != 0:
        return False, cp.stderr.strip() or "bundle create failed"
    payload["bundle_created"] = True

    cp = _run(repo, "git", "reset", "--hard", "origin/main")
    if cp.returncode != 0:
        return False, cp.stderr.strip() or "git reset --hard failed"
    payload["reset_applied"] = True
    return True, "applied"


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup-first alignment helper for diverged local main")
    parser.add_argument("--repo", default=".", help="Repo root (default: .)")
    parser.add_argument("--bundle-dir", default="/tmp", help="Bundle output directory (default: /tmp)")
    parser.add_argument("--timestamp", default=None, help="Deterministic timestamp override (UTC format)")
    parser.add_argument("--apply", action="store_true", help="Create backup+bundle and reset local main")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    payload = evaluate(repo, Path(args.bundle_dir), timestamp=args.timestamp)

    if args.apply:
        ok, msg = apply(repo, payload)
        if not ok:
            errs = payload.get("errors")
            if isinstance(errs, list):
                errs.append(msg)
            else:
                payload["errors"] = [msg]
            print(json.dumps(payload, indent=2))
            return 1

        if msg == "applied":
            refreshed = evaluate(repo, Path(args.bundle_dir), timestamp=args.timestamp)
            refreshed["backup_branch"] = payload["backup_branch"]
            refreshed["bundle_path"] = payload["bundle_path"]
            refreshed["backup_created"] = payload["backup_created"]
            refreshed["bundle_created"] = payload["bundle_created"]
            refreshed["reset_applied"] = payload["reset_applied"]
            payload = refreshed
        else:
            payload["reset_applied"] = False

    print(json.dumps(payload, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
