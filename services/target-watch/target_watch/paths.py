"""Locations: repo root, schema tree, state directory, target globs."""
from __future__ import annotations

import os
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
BUILTIN_TARGETS = SERVICE_DIR / "targets"
DEFAULT_TARGET_PATHS = "~/code/*/.watch/*.toml:~/code/33GOD/*/.watch/*.toml"


def repo_root() -> Path:
    """The bloodbank checkout this service runs from."""
    for parent in SERVICE_DIR.parents:
        if (parent / "docs" / "event-naming.md").is_file() and (parent / "schemas").is_dir():
            return parent
    return Path.home() / "code" / "33GOD" / "bloodbank"


def schemas_dir() -> Path:
    for name in ("TARGET_WATCH_SCHEMAS_DIR", "BLOODBANK_SCHEMAS_DIR"):
        if os.environ.get(name):
            return Path(os.environ[name]).expanduser()
    return repo_root() / "schemas"


def state_dir() -> Path:
    if os.environ.get("TARGET_WATCH_STATE_DIR"):
        return Path(os.environ["TARGET_WATCH_STATE_DIR"]).expanduser()
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "target-watch"


def target_globs(override: str | None = None) -> list[str]:
    """Colon-separated globs. Synthetic built-ins require explicit --targets or env opt-in."""
    raw = override if override is not None else os.environ.get("TARGET_WATCH_PATHS")
    if raw:
        return [p for p in raw.split(":") if p.strip()]
    return DEFAULT_TARGET_PATHS.split(":")


def get_path(document: object, dotted: str | None) -> object:
    """Resolve 'data.sender.handle' inside an envelope; None when any hop is missing."""
    if not dotted:
        return None
    node = document
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node
