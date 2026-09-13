"""Discover installed CLI executables under shell and user-service PATHs."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def find_cli_binary(name: str) -> str | None:
    aliases = (name, "agy") if name == "antigravity" else (name,)
    home = Path.home()
    bins = [home / ".local/bin", home / ".kimi-code/bin", home / ".opencode/bin",
            home / ".bun/bin", home / ".cargo/bin", home / ".npm-global/bin"]
    data = Path(os.environ.get("MISE_DATA_DIR") or
                (Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share") / "mise"))
    for alias in aliases:
        executable = shutil.which(alias)
        if executable:
            return executable
        candidates = [directory / alias for directory in bins]
        # npm globals reside in their managed Node version's bin directory;
        # user services do not inherit mise's activated shell PATH.
        candidates.extend(sorted((data / "installs").glob(f"*/*/bin/{alias}")))
        for path in candidates:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    return None
