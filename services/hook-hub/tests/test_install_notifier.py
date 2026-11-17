"""Isolated notifier install: npm scripts disabled, no native config or real audio."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from install_notifier import install, _move_noreplace


def test_notifier_install_dry_run_and_local_tarball(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".config/claude-notifications"
    sounds = config_dir / "sounds"
    sounds.mkdir(parents=True)
    (sounds / "claude-notification-bell.wav").write_bytes(b"fixture")
    (config_dir / "settings.json").write_text(json.dumps({"sound": True,
        "soundType": "claude-notification-bell", "desktopNotification": False}))
    command = home / ".local/bin/claude-notify"
    command.parent.mkdir(parents=True)
    command.symlink_to(home / "missing/claude-notify.js")
    original = os.readlink(command)
    # Read-only npm pack into the fixture. The package source is never mutated.
    tarball = tmp_path / "notifier.tgz"
    subprocess.run(["npm", "pack", "@delorenj/claude-notifications@2.2.1",
                    "--pack-destination", str(tmp_path), "--silent"], check=True, capture_output=True, text=True)
    packed = next(tmp_path.glob("*.tgz"))
    packed.rename(tarball)
    with tarfile.open(tarball) as archive:
        postinstall = archive.extractfile("package/postinstall.js")
        assert postinstall is not None
        post = postinstall.read().decode()
        assert "claude-notifications.js" in post and "sounds" in post
    assert not install(home, str(tarball), apply=False)["applied"]
    assert os.readlink(command) == original
    plan = install(home, str(tarball), apply=True)
    assert plan["applied"] and Path(plan["source"]).is_file()
    assert os.readlink(home / ".local/bin/claude-notify.bb37-backup") == original
    assert command.resolve() == Path(plan["source"])
    assert not (home / ".claude/settings.json").exists()  # no native install
    assert json.loads((config_dir / "settings.json").read_text())["soundType"] == "claude-notification-bell"
    stub = tmp_path / "bin"
    stub.mkdir()
    marker = tmp_path / "audio.log"
    player = stub / "paplay"
    player.write_text('#!/bin/sh\nprintf "%s\\n" "$1" > "$BB37_AUDIO"\n')
    player.chmod(0o755)
    env = dict(os.environ, HOME=str(home), BB37_AUDIO=str(marker),
               PATH=f"{stub}:{home / '.local/bin'}:{os.environ['PATH']}")
    subprocess.run([str(command)], input="{}", text=True, env=env,
                   check=True, capture_output=True, timeout=10)
    assert marker.read_text().strip() == str(sounds / "claude-notification-bell.wav")


INSTALLER = Path(__file__).resolve().parents[1] / "install_notifier.py"


def test_atomic_backup_move_refuses_late_file_and_dangling_symlink(tmp_path):
    command = tmp_path / "command"
    backup = tmp_path / "backup"
    command.symlink_to(tmp_path / "original-missing")
    for kind in ("file", "symlink"):
        if kind == "file":
            backup.write_text("rollback payload")
        else:
            backup.symlink_to(tmp_path / "another-missing")
        with pytest.raises(FileExistsError):
            _move_noreplace(command, backup)
        assert os.readlink(command) == str(tmp_path / "original-missing")
        if kind == "file":
            assert backup.read_text() == "rollback payload"
        else:
            assert os.readlink(backup) == str(tmp_path / "another-missing")
        backup.unlink()


def _race_fixture(tmp_path):
    home = tmp_path / "home"
    config = home / ".config/claude-notifications"
    config.mkdir(parents=True)
    (config / "settings.json").write_text('{"sound": false}')
    command = home / ".local/bin/claude-notify"
    command.parent.mkdir(parents=True)
    command.symlink_to(tmp_path / "original-missing")
    stub = tmp_path / "bin"
    stub.mkdir()
    node = stub / "node"
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o755)
    npm = stub / "npm"
    npm.write_text('''#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
root = Path(os.environ['BB37_FIXTURE'])
prefix = Path(sys.argv[sys.argv.index('--prefix') + 1])
source = prefix / 'node_modules/@delorenj/claude-notifications/bin/claude-notify.js'
source.parent.mkdir(parents=True, exist_ok=True)
source.write_text('#!/bin/sh\\nexit 0\\n')
source.chmod(0o755)
with (root / 'npm-calls').open('a') as log:
    log.write('call\\n')
if os.environ.get('BB37_PAUSE') == '1':
    (root / 'npm-paused').touch()
    for _ in range(200):
        if (root / 'npm-release').exists(): break
        time.sleep(.05)
    else: sys.exit(2)
''')
    npm.chmod(0o755)
    env = dict(os.environ, PATH=f"{stub}:{os.environ['PATH']}", BB37_FIXTURE=str(tmp_path))
    return home, command, env


def _wait_for(path, proc):
    for _ in range(200):
        if path.exists():
            return
        assert proc.poll() is None, "installer exited before npm pause"
        time.sleep(.025)
    pytest.fail(f"timed out waiting for {path}")


def _start(home, env):
    return subprocess.Popen([sys.executable, str(INSTALLER), "--home", str(home), "--apply"],
                            env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@pytest.mark.parametrize("backup_kind", ["file", "symlink"])
def test_late_backup_collision_never_clobbers(tmp_path, backup_kind):
    home, command, env = _race_fixture(tmp_path)
    original = os.readlink(command)
    proc = _start(home, dict(env, BB37_PAUSE="1"))
    backup = command.with_name("claude-notify.bb37-backup")
    try:
        _wait_for(tmp_path / "npm-paused", proc)
        if backup_kind == "file":
            backup.write_text("other process rollback target: MUST SURVIVE")
        else:
            backup.symlink_to(tmp_path / "other-rollback-target")
        (tmp_path / "npm-release").touch()
        out, err = proc.communicate(timeout=15)
        assert proc.returncode != 0 and "backup already exists" in err and not out
        if backup_kind == "file":
            assert backup.read_text() == "other process rollback target: MUST SURVIVE"
        else:
            assert os.readlink(backup) == str(tmp_path / "other-rollback-target")
        assert os.readlink(command) == original
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.parametrize("replacement", ["removed", "file", "working-link", "dangling-link"])
def test_command_changed_during_npm_is_not_displaced(tmp_path, replacement):
    home, command, env = _race_fixture(tmp_path)
    proc = _start(home, dict(env, BB37_PAUSE="1"))
    try:
        _wait_for(tmp_path / "npm-paused", proc)
        command.unlink()
        if replacement == "file":
            command.write_text("operator executable")
        elif replacement == "working-link":
            target = tmp_path / "operator-executable"
            target.write_text("operator executable")
            command.symlink_to(target)
        elif replacement == "dangling-link":
            command.symlink_to(tmp_path / "different-missing")
        (tmp_path / "npm-release").touch()
        out, err = proc.communicate(timeout=15)
        assert proc.returncode != 0 and "command changed during install" in err and not out
        if replacement == "removed":
            assert not command.exists() and not command.is_symlink()
        elif replacement == "file":
            assert command.read_text() == "operator executable"
        elif replacement == "working-link":
            assert os.readlink(command) == str(tmp_path / "operator-executable")
        else:
            assert os.readlink(command) == str(tmp_path / "different-missing")
        assert not command.with_name("claude-notify.bb37-backup").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_simultaneous_applies_serialize_before_npm(tmp_path):
    home, command, env = _race_fixture(tmp_path)
    original = os.readlink(command)
    first = _start(home, dict(env, BB37_PAUSE="1"))
    second = None
    try:
        _wait_for(tmp_path / "npm-paused", first)
        second = _start(home, env)
        time.sleep(.25)
        assert second.poll() is None
        assert (tmp_path / "npm-calls").read_text().splitlines() == ["call"]
        (tmp_path / "npm-release").touch()
        first_out, first_err = first.communicate(timeout=15)
        second_out, second_err = second.communicate(timeout=15)
        assert first.returncode == 0, first_err
        assert json.loads(first_out)["applied"]
        assert second.returncode != 0 and "refusing to replace a working symlink" in second_err and not second_out
        assert (tmp_path / "npm-calls").read_text().splitlines() == ["call"]
        assert os.readlink(command.with_name("claude-notify.bb37-backup")) == original
        assert command.resolve().is_file()
    finally:
        for proc in (first, second):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
