#!/usr/bin/env python3
"""Cleanup generated repo-health evidence artifacts.

Env vars:
- KEEP: optional non-negative integer; keep newest N artifacts.
- REPORT: if truthy (1/true/yes/on), emit JSON report.
- DRY_RUN: if truthy, compute and report actions but do not delete files.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    evidence_dir = Path("_bmad_output/evidence")
    # Keep cleanup scoped to primary timestamped snapshots only.
    # Decision artifacts (repo-health-idle-decision-*.json) are intentionally excluded.
    files = sorted(evidence_dir.glob("repo-health-20*.json")) if evidence_dir.exists() else []

    keep_raw = os.getenv("KEEP")
    keep: int | None = None
    if keep_raw not in (None, ""):
        try:
            keep = int(keep_raw)
        except ValueError:
            print("KEEP must be a non-negative integer", file=sys.stderr)
            return 2
        if keep < 0:
            print("KEEP must be a non-negative integer", file=sys.stderr)
            return 2

    if keep is None:
        kept = []
        removed = files
    elif keep == 0:
        kept = []
        removed = files
    elif keep >= len(files):
        kept = files
        removed = []
    else:
        split = len(files) - keep
        removed = files[:split]
        kept = files[split:]

    dry_run = _truthy(os.getenv("DRY_RUN"))
    if not dry_run:
        for path in removed:
            path.unlink(missing_ok=True)

    report = {
        "pattern": "_bmad_output/evidence/repo-health-20*.json",
        "total_before": len(files),
        "removed_count": len(removed),
        "kept_count": len(kept),
        "removed_paths": [str(p) for p in removed],
        "kept_paths": [str(p) for p in kept],
        "keep_requested": keep,
        "dry_run": dry_run,
    }

    if _truthy(os.getenv("REPORT")):
        print(json.dumps(report, indent=2))
    else:
        verb = "would remove" if dry_run else "removed"
        if keep is None:
            print(f"{verb} {len(removed)} repo-health artifacts")
        else:
            print(f"{verb} {len(removed)} repo-health artifacts (kept {len(kept)})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
