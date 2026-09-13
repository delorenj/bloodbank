#!/usr/bin/env python3
"""Read the completed native-cutover manifest without contacting the daemon.

Legacy installers/scripts use this to avoid reintroducing a second behavioral
owner. Only cutover.py's explicit activation step writes this manifest. A hub
child is the registered owner and must be allowed to run the same old script.
"""
from __future__ import annotations

import argparse
import json
import os
import tomllib
from pathlib import Path


def owns(handler: str, cli: str = "") -> bool:
    if os.environ.get("BB_HOOK_HUB") == "off":
        return False
    path = Path(os.environ.get("BB_HOOK_OWNERSHIP", Path.home() / ".config/33god/hook-hub/ownership.json"))
    try:
        data = json.loads(path.read_text())
        if data.get("version") != 1 or handler not in data.get("handler_ids", []):
            return False
        if cli and cli not in data.get("clis", []):
            return False
        registry = tomllib.loads(Path(data["registry"]).read_text())
        # A paused row still owns its concern. Otherwise an old installer can
        # silently re-enable the behavior outside the hub's pause control.
        return any(row.get("id") == handler
                   and (not cli or not row.get("clis") or cli in row["clis"])
                   for row in registry.get("handler", []))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("handler")
    parser.add_argument("--cli", default="")
    args = parser.parse_args()
    return 0 if owns(args.handler, args.cli) else 1


if __name__ == "__main__":
    raise SystemExit(main())
