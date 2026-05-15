#!/usr/bin/env python3
"""Strict-clean preflight gate for BMAD operator loops.

Runs repo-health strict mode and returns a compact JSON contract suitable for
automation loops before mutating actions (branching/commits/PR merges).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def _run_repo_health_strict(repo: Path) -> subprocess.CompletedProcess[str]:
    cli = repo / "cli" / "bb.py"
    return subprocess.run(
        [sys.executable, str(cli), "repo-health", "--json", "--require-clean-worktree"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(repo),
    )


def evaluate(repo: Path) -> tuple[int, dict[str, Any]]:
    cp = _run_repo_health_strict(repo)

    payload: dict[str, Any] = {
        "ok": False,
        "repo": str(repo),
        "gate": "strict_clean_preflight",
        "worktree_dirty": None,
        "git_status": None,
        "errors": [],
        "blocking_reason": "",
    }

    raw = (cp.stdout or "").strip()
    if not raw:
        payload["blocking_reason"] = cp.stderr.strip() or "repo-health strict returned empty output"
        return 1, payload

    try:
        snapshot = json.loads(raw)
    except json.JSONDecodeError:
        payload["blocking_reason"] = "invalid JSON from repo-health strict"
        payload["errors"] = [cp.stderr.strip() or "json decode failed"]
        return 1, payload

    payload["worktree_dirty"] = snapshot.get("worktree_dirty")
    payload["git_status"] = snapshot.get("git_status")
    errors = snapshot.get("errors")
    payload["errors"] = errors if isinstance(errors, list) else ["repo-health strict returned invalid errors field"]

    if cp.returncode == 0:
        payload["ok"] = True
        return 0, payload

    payload["blocking_reason"] = (
        "worktree_dirty"
        if payload["worktree_dirty"] is True
        else (cp.stderr.strip() or "strict preflight failed")
    )
    return 1, payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict-clean BMAD preflight JSON helper")
    parser.add_argument("--repo", default=".", help="Repository root (default: .)")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    rc, payload = evaluate(repo)
    print(json.dumps(payload, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
