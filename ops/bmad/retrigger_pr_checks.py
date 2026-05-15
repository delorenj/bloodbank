#!/usr/bin/env python3
"""Operator-safe PR check retrigger helper.

Dispatches the CI workflow for the head ref of a pull request without pushing a
new commit.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def run_json(cmd: list[str]) -> Any:
    result = run(cmd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "command failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON output from command: {' '.join(cmd)}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retrigger CI checks for a PR by dispatching workflow_dispatch on its head branch."
    )
    parser.add_argument("pr", type=int, help="Pull request number")
    parser.add_argument(
        "--workflow",
        default="ci.yml",
        help="Workflow file name to dispatch (default: ci.yml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve target branch and print actions without dispatching",
    )
    args = parser.parse_args()

    repo = run_json(["gh", "repo", "view", "--json", "nameWithOwner"])["nameWithOwner"]
    pr = run_json(
        [
            "gh",
            "pr",
            "view",
            str(args.pr),
            "--json",
            "number,state,headRefName,url",
        ]
    )

    payload: dict[str, Any] = {
        "repository": repo,
        "pr": pr["number"],
        "pr_state": pr["state"],
        "pr_url": pr["url"],
        "head_ref": pr["headRefName"],
        "workflow": args.workflow,
        "dry_run": args.dry_run,
        "dispatch_requested": False,
        "dispatch_exit": None,
        "dispatch_stderr": "",
        "followup_commands": [
            f"gh pr checks {args.pr}",
            f"gh run list --workflow CI --branch {pr['headRefName']} --event workflow_dispatch --limit 3",
        ],
    }

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    dispatch = run(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repo}/actions/workflows/{args.workflow}/dispatches",
            "-f",
            f"ref={pr['headRefName']}",
        ]
    )
    payload["dispatch_requested"] = True
    payload["dispatch_exit"] = dispatch.returncode
    payload["dispatch_stderr"] = dispatch.stderr.strip()

    if dispatch.returncode != 0:
        print(json.dumps(payload, indent=2))
        return dispatch.returncode

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}))
        raise SystemExit(1)
