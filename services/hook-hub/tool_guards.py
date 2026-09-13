#!/usr/bin/env python3
"""Keep optional tool installers from restoring centrally owned native hooks."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ownership import owns

SERVICE_DIR = Path(__file__).resolve().parent
GUARD = SERVICE_DIR / "tool-guards/code-review-graph"


def code_review_graph_binary(home: Path) -> Path:
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    root = Path(os.environ.get("UV_TOOL_DIR", data / "uv/tools"))
    return root / "code-review-graph/bin/code-review-graph"


def code_review_graph_main() -> int:
    arguments = sys.argv[1:]
    # Upstream documents init as an alias for install. Both write native hooks
    # by default. Hub children deliberately retain the standalone CLI behavior.
    if (arguments and arguments[0] in {"install", "init"}
            and "--no-hooks" not in arguments and owns("code-review-graph-update")):
        arguments.append("--no-hooks")
    binary = code_review_graph_binary(Path.home())
    if binary.resolve() == GUARD:
        print("code-review-graph: the uv tool entry point resolves to its guard", file=sys.stderr)
        return 126
    try:
        os.execv(str(binary), [str(binary), *arguments])
    except OSError as error:
        print(f"code-review-graph: cannot execute {binary}: {error.strerror}", file=sys.stderr)
        return 127 if isinstance(error, FileNotFoundError) else 126


def install(home: Path) -> dict:
    """Replace only the known uv launcher, without changing the tool package."""
    binary = code_review_graph_binary(home)
    path = home / ".local/bin/code-review-graph"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return {"tool": "code-review-graph", "status": "not_installed"}
    if path.is_symlink() and path.resolve() == GUARD:
        return {"tool": "code-review-graph", "status": "current", "path": str(path)}
    if (path.exists() or path.is_symlink()) and path.resolve() != binary.resolve():
        raise RuntimeError(f"refusing to replace an unrelated command: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.hook-hub-{os.getpid()}")
    try:
        temporary.symlink_to(GUARD)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"tool": "code-review-graph", "status": "linked", "path": str(path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args()
    print(json.dumps(install(args.home)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
