#!/usr/bin/env python3
"""Read-only GitHub status helper with bounded retry for transient API failures."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any


def _is_transient(err: str) -> bool:
    err_l = err.lower()
    return (
        "error connecting to api.github.com" in err_l
        or "connection reset" in err_l
        or "timed out" in err_l
    )


def _run_once(argv: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(argv, text=True, capture_output=True, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def run_with_retry(argv: list[str], attempts: int = 3) -> tuple[int, str, str, int]:
    rc, out, err = 1, "", ""
    for attempt in range(1, attempts + 1):
        rc, out, err = _run_once(argv)
        if rc == 0:
            return rc, out, err, attempt
        if attempt == attempts or not _is_transient(err):
            return rc, out, err, attempt
        time.sleep(0.5 * attempt)
    return rc, out, err, attempts


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only gh status helper with bounded retries")
    sub = parser.add_subparsers(dest="command", required=True)

    p_issue = sub.add_parser("issue-view", help="Fetch issue status JSON with retry")
    p_issue.add_argument("issue")

    p_pr = sub.add_parser("pr-view", help="Fetch PR status JSON with retry")
    p_pr.add_argument("pr")

    args = parser.parse_args()

    if args.command == "issue-view":
        cmd = ["gh", "issue", "view", str(args.issue), "--json", "number,title,state,url,updatedAt"]
    else:
        cmd = [
            "gh",
            "pr",
            "view",
            str(args.pr),
            "--json",
            "number,title,state,url,headRefName,mergeStateStatus,statusCheckRollup,updatedAt",
        ]

    rc, out, err, attempts = run_with_retry(cmd)
    payload: dict[str, Any] = {
        "ok": rc == 0,
        "attempts": attempts,
        "command": args.command,
        "stderr": err,
        "data": None,
    }

    if rc == 0 and out:
        try:
            payload["data"] = json.loads(out)
        except json.JSONDecodeError:
            payload["ok"] = False
            payload["stderr"] = "invalid JSON from gh command"
            print(json.dumps(payload, indent=2))
            return 1

    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
