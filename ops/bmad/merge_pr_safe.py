#!/usr/bin/env python3
"""Safely merge a PR for operator loops, tolerating linked-worktree delete edge cases.

- Enforces strict-clean preflight gate by default (`preflight_strict_clean.py`).
- Supports explicit `--bypass-preflight` override for intentional/manual exceptions.
- Attempts squash merge + remote branch delete.
- Verifies merged state independently via `gh pr view`.
- Treats local branch/worktree cleanup as best-effort follow-up.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CmdResult:
    code: int
    out: str
    err: str


def run(*args: str) -> CmdResult:
    cp = subprocess.run(args, text=True, capture_output=True)
    return CmdResult(code=cp.returncode, out=cp.stdout.strip(), err=cp.stderr.strip())


def gh_pr_view(pr: str) -> dict[str, object]:
    helper = Path(__file__).with_name("gh_readonly_status.py")
    cp = subprocess.run(
        [sys.executable, str(helper), "pr-view", pr],
        text=True,
        capture_output=True,
        check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "gh pr view failed")

    try:
        payload = json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid JSON from gh_readonly_status pr-view") from exc

    data = payload.get("data")
    if not payload.get("ok") or not isinstance(data, dict):
        raise RuntimeError(str(payload.get("stderr") or "gh_readonly_status returned no data"))

    return {
        "number": data.get("number"),
        "state": data.get("state"),
        "mergedAt": data.get("mergedAt"),
        "url": data.get("url"),
        "headRefName": data.get("headRefName"),
    }


def branch_exists_local(branch: str) -> bool:
    cp = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        text=True,
        capture_output=True,
    )
    return cp.returncode == 0


def worktree_paths_for_branch(branch: str) -> list[str]:
    cp = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        check=True,
        text=True,
        capture_output=True,
    )
    paths: list[str] = []
    current_path: str | None = None
    current_branch: str | None = None

    for line in cp.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line.split(" ", 1)[1].strip()
            current_branch = None
        elif line.startswith("branch "):
            current_branch = line.split(" ", 1)[1].strip().removeprefix("refs/heads/")
            if current_path and current_branch == branch:
                paths.append(current_path)

    return paths


def run_preflight(repo: Path) -> tuple[int, dict[str, object]]:
    helper = Path(__file__).with_name("preflight_strict_clean.py")
    cp = subprocess.run(
        [sys.executable, str(helper), "--repo", str(repo)],
        text=True,
        capture_output=True,
        check=False,
    )

    raw = (cp.stdout or "").strip()
    if not raw:
        return cp.returncode or 1, {
            "ok": False,
            "gate": "strict_clean_preflight",
            "blocking_reason": cp.stderr.strip() or "empty preflight output",
        }

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return cp.returncode or 1, {
            "ok": False,
            "gate": "strict_clean_preflight",
            "blocking_reason": "invalid JSON from preflight helper",
            "stderr": cp.stderr.strip(),
        }

    return cp.returncode, payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe PR merge helper for operator loops")
    parser.add_argument("pr", help="PR number or URL")
    parser.add_argument(
        "--bypass-preflight",
        action="store_true",
        help="Bypass strict-clean preflight gate (use only for intentional/manual override)",
    )
    args = parser.parse_args()

    preflight_payload: dict[str, object] = {"ok": None, "bypassed": bool(args.bypass_preflight)}
    if not args.bypass_preflight:
        preflight_rc, preflight_payload = run_preflight(Path.cwd())
        if preflight_rc != 0:
            report = {
                "pr": args.pr,
                "url": None,
                "state": "BLOCKED",
                "mergedAt": None,
                "merge_command_exit": None,
                "merge_command_stderr": "",
                "head_branch": None,
                "preflight": preflight_payload,
                "cleanup": {
                    "local_branch_status": "not_applicable",
                    "local_branch_deleted": None,
                    "linked_worktrees": [],
                    "followup_commands": [],
                },
            }
            print(json.dumps(report, indent=2))
            return 1

    merge = run("gh", "pr", "merge", args.pr, "--squash", "--delete-branch")

    try:
        view = gh_pr_view(args.pr)
    except RuntimeError as exc:
        print(f"pr_view_error: {exc}", file=sys.stderr)
        if merge.out:
            print(merge.out)
        if merge.err:
            print(merge.err, file=sys.stderr)
        return merge.code or 1

    state = str(view.get("state") or "")
    merged_at = view.get("mergedAt")
    pr_url = str(view.get("url") or "")
    head_branch = str(view.get("headRefName") or "")

    merged = state == "MERGED" and bool(merged_at)

    report: dict[str, object] = {
        "pr": view.get("number"),
        "url": pr_url,
        "state": state,
        "mergedAt": merged_at,
        "merge_command_exit": merge.code,
        "merge_command_stderr": merge.err,
        "head_branch": head_branch,
        "preflight": preflight_payload,
        "cleanup": {
            "local_branch_status": "not_applicable",
            "local_branch_deleted": None,
            "linked_worktrees": [],
            "followup_commands": [],
        },
    }

    if not merged:
        print(json.dumps(report, indent=2))
        return merge.code or 1

    cleanup = report["cleanup"]
    if head_branch:
        if not branch_exists_local(head_branch):
            cleanup["local_branch_status"] = "already_absent"
            cleanup["local_branch_deleted"] = True
        else:
            del_res = run("git", "branch", "-d", head_branch)
            if del_res.code == 0:
                cleanup["local_branch_status"] = "deleted"
                cleanup["local_branch_deleted"] = True
            else:
                cleanup["local_branch_status"] = "failed"
                cleanup["local_branch_deleted"] = False
                paths = worktree_paths_for_branch(head_branch)
                cleanup["linked_worktrees"] = paths
                followups: list[str] = []
                for path in paths:
                    followups.append(f"git worktree remove {path}")
                followups.append(f"git branch -d {head_branch}")
                cleanup["followup_commands"] = followups

    # Success even if merge command failed, as long as PR is confirmed merged.
    # This covers linked-worktree local deletion failures from gh merge.
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
