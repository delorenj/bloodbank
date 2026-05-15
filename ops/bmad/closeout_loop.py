#!/usr/bin/env python3
"""Unified BMAD loop closeout helper.

Combines:
1) safe PR merge + merged-state verification,
2) cleanup follow-up visibility (linked worktree/local branch),
3) read-only primary-checkout drift snapshot evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _run_json(command: list[str]) -> tuple[int, dict[str, object] | None, str, str]:
    cp = subprocess.run(command, text=True, capture_output=True)
    out = cp.stdout.strip()
    err = cp.stderr.strip()
    payload: dict[str, object] | None = None
    if out:
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            payload = None
    return cp.returncode, payload, out, err


def _is_git_repo(path: Path) -> bool:
    cp = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        text=True,
        capture_output=True,
    )
    return cp.returncode == 0 and cp.stdout.strip() == "true"


def _resolve_primary_repo(cli_value: str | None) -> tuple[Path, str]:
    if cli_value:
        return Path(cli_value), "cli"

    env_value = os.getenv("PRIMARY_REPO")
    if env_value:
        return Path(env_value), "env:PRIMARY_REPO"

    return Path.cwd(), "cwd"


def _emit(report: dict[str, Any], out: str | None) -> None:
    rendered = json.dumps(report, indent=2)
    print(rendered)
    if out:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified BMAD closeout helper")
    parser.add_argument("pr", help="PR number or URL")
    parser.add_argument(
        "--primary-repo",
        help="Path to primary checkout for read-only drift evidence snapshot (optional: defaults PRIMARY_REPO env, then cwd)",
    )
    parser.add_argument("--out", help="Optional path to also write JSON report")
    args = parser.parse_args()

    primary_repo, primary_repo_source = _resolve_primary_repo(args.primary_repo)

    if not _is_git_repo(primary_repo):
        report = {
            "pr": args.pr,
            "primary_repo": str(primary_repo.resolve()),
            "primary_repo_source": primary_repo_source,
            "overall_status": "error",
            "warnings": ["resolved primary repo is not a git worktree"],
            "diagnostics": {
                "merge_rc": None,
                "drift_rc": None,
                "merge_stderr": "",
                "drift_stderr": "",
                "merge_stdout_raw": None,
                "drift_stdout_raw": None,
            },
        }
        _emit(report, args.out)
        return 1

    merge_cmd = ["python3", "ops/bmad/merge_pr_safe.py", args.pr]
    merge_rc, merge_payload, merge_out, merge_err = _run_json(merge_cmd)

    drift_cmd = [
        "python3",
        "ops/repo-health/drift_snapshot.py",
        "--repo",
        str(primary_repo),
        "--json",
    ]
    drift_rc, drift_payload, drift_out, drift_err = _run_json(drift_cmd)

    merged = bool(merge_payload and merge_payload.get("state") == "MERGED" and merge_payload.get("mergedAt"))
    drift_ok = drift_rc == 0 and drift_payload is not None

    warnings: list[str] = []
    cleanup_followups: list[str] = []
    cleanup_local_branch_status = "unknown"
    cleanup_local_branch_deleted: bool | None = None
    if merge_payload:
        cleanup = merge_payload.get("cleanup")
        if isinstance(cleanup, dict):
            status = cleanup.get("local_branch_status")
            if isinstance(status, str) and status:
                cleanup_local_branch_status = status
            local_deleted = cleanup.get("local_branch_deleted")
            if isinstance(local_deleted, bool) or local_deleted is None:
                cleanup_local_branch_deleted = local_deleted
            if local_deleted is False:
                warnings.append("local branch cleanup requires follow-up")
            cmds = cleanup.get("followup_commands")
            if isinstance(cmds, list):
                cleanup_followups = [str(c) for c in cmds]

    if merge_rc != 0 and merged:
        warnings.append("merge command returned non-zero but PR is confirmed merged")

    if not drift_ok:
        warnings.append("primary drift snapshot failed")

    report: dict[str, object] = {
        "pr": args.pr,
        "primary_repo": str(primary_repo.resolve()),
        "primary_repo_source": primary_repo_source,
        "merged": merged,
        "drift_snapshot_ok": drift_ok,
        "merge": merge_payload,
        "drift": drift_payload,
        "cleanup_local_branch_status": cleanup_local_branch_status,
        "cleanup_local_branch_deleted": cleanup_local_branch_deleted,
        "cleanup_followup_commands": cleanup_followups,
        "warnings": warnings,
        "overall_status": "ok" if (merged and drift_ok) else "error",
        "diagnostics": {
            "merge_rc": merge_rc,
            "drift_rc": drift_rc,
            "merge_stderr": merge_err,
            "drift_stderr": drift_err,
            "merge_stdout_raw": merge_out if merge_payload is None else None,
            "drift_stdout_raw": drift_out if drift_payload is None else None,
        },
    }

    _emit(report, args.out)
    return 0 if (merged and drift_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
