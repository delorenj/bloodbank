#!/usr/bin/env python3
"""Read-only git drift snapshot for operator loop evidence.

Reports branch divergence + working-tree drift summary without mutating state.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path


def _run_git(repo: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return cp.stdout.strip("\n")


def _parse_branch_status(line: str) -> tuple[str, str | None, int, int]:
    # Examples:
    # ## main
    # ## main...origin/main
    # ## main...origin/main [ahead 1, behind 3]
    payload = line.removeprefix("## ")
    branch = payload
    upstream = None
    ahead = 0
    behind = 0

    if "..." in payload:
        branch, rest = payload.split("...", 1)
        branch = branch.strip()
        if " [" in rest and rest.endswith("]"):
            upstream, meta = rest.split(" [", 1)
            upstream = upstream.strip() or None
            meta = meta[:-1]
            parts = [p.strip() for p in meta.split(",")]
            for part in parts:
                if part.startswith("ahead "):
                    ahead = int(part.split(" ", 1)[1])
                elif part.startswith("behind "):
                    behind = int(part.split(" ", 1)[1])
        else:
            upstream = rest.strip() or None
    else:
        branch = payload.strip()

    return branch, upstream, ahead, behind


def _path_from_status_line(line: str) -> str | None:
    if not line:
        return None
    if line.startswith("## "):
        return None

    # porcelain short format: XY <path>
    body = line[3:] if len(line) >= 4 else ""
    if not body:
        return None

    # Renames are rendered as "old -> new".
    if " -> " in body:
        body = body.split(" -> ", 1)[1]

    return body.strip() or None


def snapshot(repo: Path) -> dict[str, object]:
    status = _run_git(repo, "status", "--short", "--branch")
    lines = status.splitlines()

    if not lines:
        raise RuntimeError("empty git status output")

    branch, upstream, ahead, behind = _parse_branch_status(lines[0])

    tracked_modified = 0
    untracked = 0
    buckets: Counter[str] = Counter()

    for line in lines[1:]:
        if not line:
            continue
        code = line[:2]
        if code == "??":
            untracked += 1
        else:
            tracked_modified += 1

        path = _path_from_status_line(line)
        if path:
            top = path.split("/", 1)[0]
            buckets[top] += 1

    return {
        "repo": str(repo.resolve()),
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "tracked_modified_count": tracked_modified,
        "untracked_count": untracked,
        "top_level_path_buckets": dict(sorted(buckets.items())),
    }


def render_text(data: dict[str, object]) -> str:
    lines: list[str] = []
    lines.append(f"repo: {data['repo']}")
    lines.append(f"branch: {data['branch']}")
    lines.append(f"upstream: {data['upstream'] or 'none'}")
    lines.append(f"ahead: {data['ahead']}")
    lines.append(f"behind: {data['behind']}")
    lines.append(f"tracked_modified_count: {data['tracked_modified_count']}")
    lines.append(f"untracked_count: {data['untracked_count']}")
    lines.append("top_level_path_buckets:")

    buckets: dict[str, int] = data["top_level_path_buckets"]  # type: ignore[assignment]
    if not buckets:
        lines.append("- none")
    else:
        for key, count in buckets.items():
            lines.append(f"- {key}: {count}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only git drift snapshot")
    parser.add_argument("--repo", default=".", help="Path to git repository (default: .)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args()

    repo = Path(args.repo)
    data = snapshot(repo)

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(render_text(data))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
