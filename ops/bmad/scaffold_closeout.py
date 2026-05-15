#!/usr/bin/env python3
"""Scaffold BMAD ticket closeout artifacts from template.

Env vars:
- ISSUE_ID: required positive integer ticket id.
- ISSUE_TITLE: optional title to prefill.
- OWNER: optional owner/agent to prefill.
- OVERWRITE: truthy to allow replacing an existing closeout file.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _require_issue_id(raw: str | None) -> str:
    if raw is None or not raw.strip():
        raise ValueError("ISSUE_ID is required (example: ISSUE_ID=98)")
    candidate = raw.strip()
    if not re.fullmatch(r"[1-9]\d*", candidate):
        raise ValueError("ISSUE_ID must be a positive integer")
    return candidate


def main() -> int:
    try:
        issue_id = _require_issue_id(os.getenv("ISSUE_ID"))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    issue_title = os.getenv("ISSUE_TITLE", "<title>").strip() or "<title>"
    owner = os.getenv("OWNER", "<name/agent>").strip() or "<name/agent>"

    template_path = Path("_bmad_output/templates/ticket-closeout.md")
    if not template_path.exists():
        print(f"template missing: {template_path}", file=sys.stderr)
        return 2

    out_path = Path(f"_bmad_output/issue-{issue_id}-execution.md")
    if out_path.exists() and not _truthy(os.getenv("OVERWRITE")):
        print(
            f"closeout already exists: {out_path} (set OVERWRITE=1 to replace)",
            file=sys.stderr,
        )
        return 3

    content = template_path.read_text(encoding="utf-8")
    content = content.replace("<id>", issue_id)
    content = content.replace("<title>", issue_title)
    content = content.replace("<name/agent>", owner)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")

    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
