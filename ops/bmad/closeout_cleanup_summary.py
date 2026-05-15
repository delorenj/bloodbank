#!/usr/bin/env python3
"""Summarize BMAD closeout artifacts with cleanup status highlights."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _extract_cleanup(payload: dict[str, Any]) -> tuple[str, bool | None, int]:
    status = payload.get("cleanup_local_branch_status")
    deleted = payload.get("cleanup_local_branch_deleted")
    followups = payload.get("cleanup_followup_commands")

    if not isinstance(status, str) or not status:
        merge = payload.get("merge")
        if isinstance(merge, dict):
            cleanup = merge.get("cleanup")
            if isinstance(cleanup, dict):
                nested_status = cleanup.get("local_branch_status")
                if isinstance(nested_status, str) and nested_status:
                    status = nested_status
                nested_deleted = cleanup.get("local_branch_deleted")
                if isinstance(nested_deleted, bool) or nested_deleted is None:
                    deleted = nested_deleted
                if not isinstance(followups, list):
                    nested_followups = cleanup.get("followup_commands")
                    if isinstance(nested_followups, list):
                        followups = nested_followups

    if not isinstance(status, str) or not status:
        status = "unknown"

    if not isinstance(deleted, bool) and deleted is not None:
        deleted = None

    followup_count = len(followups) if isinstance(followups, list) else 0
    return status, deleted, followup_count


def summarize(evidence_dir: Path, limit: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = []

    for path in sorted(evidence_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        if not isinstance(payload, dict):
            continue

        if "overall_status" not in payload and "merge" not in payload:
            continue

        status, deleted, followup_count = _extract_cleanup(payload)

        if status == "unknown" and "cleanup_local_branch_status" not in payload and not isinstance(payload.get("merge"), dict):
            continue

        warnings = payload.get("warnings")
        warning_count = len(warnings) if isinstance(warnings, list) else 0

        items.append(
            {
                "artifact": str(path),
                "pr": payload.get("pr"),
                "overall_status": payload.get("overall_status"),
                "merged": payload.get("merged"),
                "cleanup_local_branch_status": status,
                "cleanup_local_branch_deleted": deleted,
                "cleanup_followup_count": followup_count,
                "warning_count": warning_count,
            }
        )

        if len(items) >= limit:
            break

    return {
        "evidence_dir": str(evidence_dir.resolve()),
        "count": len(items),
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize closeout artifact cleanup status")
    parser.add_argument("--evidence-dir", default="_bmad_output/evidence", help="Directory containing closeout JSON artifacts")
    parser.add_argument("--limit", type=int, default=10, help="Maximum artifact rows to emit")
    args = parser.parse_args()

    evidence_dir = Path(args.evidence_dir)
    if not evidence_dir.exists() or not evidence_dir.is_dir():
        print(json.dumps({"error": f"evidence dir not found: {evidence_dir}"}, indent=2))
        return 1

    limit = max(1, args.limit)
    report = summarize(evidence_dir, limit)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
