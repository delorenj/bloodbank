"""cwd -> ticket board, by walking up to the nearest .project.json.

STOPS at the first .project.json found, even when that manifest carries no
board. There is no inheritance: `momo/` and `candybar/` have manifests with no
ticket_provider, and those agents must resolve to NO board rather than silently
inheriting 33GOD's. Four registered projects live under 33GOD, so a naive
`cwd.startswith(repo_path)` hands every submodule agent to the parent board.

Stdlib only; ~15us per call.
"""
from __future__ import annotations

import json
from pathlib import Path

MANIFEST = ".project.json"


def find_manifest(start: str | Path) -> Path | None:
    """Nearest .project.json at or above *start*."""
    try:
        here = Path(start).resolve()
    except (OSError, RuntimeError):
        return None
    if here.is_file():
        here = here.parent
    for directory in (here, *here.parents):
        candidate = directory / MANIFEST
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def board_for(cwd: str | Path) -> dict | None:
    """Return {board_id, identifier, workspace, repo_path, slug} or None.

    None means "this directory has no board", which is a real and common answer
    -- not an error. A manifest that fails to parse also returns None rather
    than climbing past it: a broken manifest must not silently promote an agent
    onto its parent's board.
    """
    manifest = find_manifest(cwd)
    if manifest is None:
        return None
    try:
        data = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    provider = data.get("ticket_provider")
    if not isinstance(provider, dict) or not provider.get("board_id"):
        return None
    return {
        "board_id": provider.get("board_id", ""),
        "identifier": provider.get("identifier", ""),
        "workspace": provider.get("workspace", ""),
        "slug": data.get("project_slug", ""),
        "repo_path": str(manifest.parent),
    }
