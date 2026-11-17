#!/usr/bin/env python3
"""Install only claude-notify for hook-hub; never install native CLI hooks."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

PACKAGE = "@delorenj/claude-notifications@2.2.1"


def install(home: Path, package: str, *, apply: bool) -> dict:
    home = home.expanduser().resolve()
    prefix = home / ".local/share/33god/claude-notifications"
    command = home / ".local/bin/claude-notify"
    source = prefix / "node_modules/@delorenj/claude-notifications/bin/claude-notify.js"
    old = command.with_name("claude-notify.bb37-backup")
    if command.exists() and not command.is_symlink():
        raise RuntimeError(f"refusing to overwrite a non-symlink executable: {command}")
    if command.is_symlink() and command.exists():
        raise RuntimeError(f"refusing to replace a working symlink: {command}")
    if old.exists() or old.is_symlink():
        raise RuntimeError(f"backup already exists: {old}")
    if not shutil.which("node") or not shutil.which("npm"):
        raise RuntimeError("node and npm must resolve in the service environment")
    settings = home / ".config/claude-notifications/settings.json"
    if not settings.is_file():
        raise RuntimeError(f"notification settings absent: {settings}")
    config = json.loads(settings.read_text())
    if config.get("sound"):
        sound = home / ".config/claude-notifications/sounds" / (str(config.get("soundType", "claude-notification")) + ".wav")
        if not sound.is_file():
            raise RuntimeError(f"sound asset absent (postinstall is intentionally disabled): {sound}")
    plan = {"package": package, "source": str(source), "link": str(command),
            "backup": str(old) if command.is_symlink() else None, "applied": False}
    if not apply:
        return plan
    env = dict(os.environ, HOME=str(home), npm_config_cache=str(prefix / ".npm-cache"))
    subprocess.run(["npm", "install", "--prefix", str(prefix), "--ignore-scripts", "--no-audit",
                    "--no-fund", "--save=false", package], env=env, check=True)
    if not source.is_file() or not os.access(source, os.X_OK):
        raise RuntimeError(f"installed notification binary unavailable: {source}")
    command.parent.mkdir(parents=True, exist_ok=True)
    if command.is_symlink():
        command.rename(old)
    try:
        command.symlink_to(source)
    except Exception:
        if old.is_symlink():
            old.rename(command)
        raise
    plan["applied"] = True
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--package", default=PACKAGE, help="pinned package (local tarball for isolated test)")
    parser.add_argument("--apply", action="store_true", help="perform the install after review; default is dry run")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.home, args.package, apply=args.apply)))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"notification installation refused: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
