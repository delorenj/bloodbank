#!/usr/bin/env python3
"""Install only claude-notify for hook-hub; never install native CLI hooks."""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

PACKAGE = "@delorenj/claude-notifications@2.2.2"


def _move_noreplace(source: Path, destination: Path) -> None:
    """Linux renameat2: atomically move without replacing even a dangling symlink."""
    # Linux's no-replace rename is required here; do not fall back to a clobbering move.
    libc = ctypes.CDLL(None, use_errno=True)
    at_fdcwd = -100
    rename_noreplace = 1
    if libc.renameat2(at_fdcwd, os.fsencode(source), at_fdcwd,
                      os.fsencode(destination), rename_noreplace) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def _identity(path: Path) -> tuple[int, int, str] | None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(entry.st_mode):
        return entry.st_dev, entry.st_ino, "<non-symlink>"
    return entry.st_dev, entry.st_ino, os.readlink(path)


def install(home: Path, package: str, *, apply: bool) -> dict:
    home = home.expanduser().resolve()
    if not apply:
        return _install_locked(home, package, apply=False)
    lock = home / ".local/share/33god/claude-notifications.install.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return _install_locked(home, package, apply=True)


def _install_locked(home: Path, package: str, *, apply: bool) -> dict:
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
    original = _identity(command) if command.is_symlink() else None
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
    if old.exists() or old.is_symlink():
        raise RuntimeError(f"backup already exists: {old}")
    if original != _identity(command):
        raise RuntimeError(f"command changed during install: {command}")
    if original is not None:
        try:
            _move_noreplace(command, old)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise RuntimeError(f"backup already exists: {old}") from exc
            raise
        if _identity(old) != original:
            if not command.exists() and not command.is_symlink():
                _move_noreplace(old, command)
            raise RuntimeError(f"command changed during backup transition; inspect {command} and {old}")
    try:
        command.symlink_to(source)
    except Exception:
        if original is not None and _identity(old) == original and not command.exists() and not command.is_symlink():
            _move_noreplace(old, command)
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
