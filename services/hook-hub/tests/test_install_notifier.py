"""Isolated notifier install: npm scripts disabled, no native config or real audio."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from install_notifier import install


def test_notifier_install_dry_run_and_local_tarball(tmp_path, monkeypatch):
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
