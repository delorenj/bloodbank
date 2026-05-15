#!/usr/bin/env python3
"""Safely merge a PR for operator loops, tolerating linked-worktree delete edge cases.

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


@dataclass
class CmdResult:
    code: int
    out: str
    err: str


def run(*args: str) -> CmdResult:
    cp = subprocess.run(args, text=True, capture_output=True)
    return CmdResult(code=cp.returncode, out=cp.stdout.strip(), err=cp.stderr.strip())


def gh_pr_view(pr: str) -> dict[str, object]:
    cp = subprocess.run(
        ["gh", "pr", "view", pr, "--json", "number,state,mergedAt,url,headRefName"],
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(cp.stdout)


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe PR merge helper for operator loops")
    parser.add_argument("pr", help="PR number or URL")
    args = parser.parse_args()

    merge = run("gh", "pr", "merge", args.pr, "--squash", "--delete-branch")

    try:
        view = gh_pr_view(args.pr)
    except subprocess.CalledProcessError as exc:
        print(f"pr_view_error: {exc.stderr.strip() or exc}", file=sys.stderr)
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
