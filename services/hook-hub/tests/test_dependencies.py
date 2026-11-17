from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hub
import concerns
from dependencies import missing_dependencies


def fixture(tmp_path: Path):
    home = tmp_path / "home"
    local = home / ".local/bin"
    shims = home / ".local/share/mise/shims"
    local.mkdir(parents=True)
    shims.mkdir(parents=True)
    return home, local, shims


def executable(path: Path, source: str):
    path.write_text(source)
    path.chmod(0o755)


def test_minimal_service_path_is_shared_by_lookup_and_child(tmp_path, monkeypatch):
    home, local, shims = fixture(tmp_path)
    monkeypatch.setattr(hub.Path, "home", lambda: home)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    executable(local / "codegraph", "#!/bin/sh\nexit 0\n")
    req = {"cli": "claude", "native": "UserPromptSubmit", "env": {"PATH": "/hostile/bin", "DISPLAY": ":1"}}
    env = hub._child_env(req, "prompt_submit")
    assert env["PATH"] == f"{local}:{shims}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    assert env["DISPLAY"] == ":1"
    assert subprocess.run([sys.executable, "-c", "import shutil,sys;sys.stdout.write(shutil.which('codegraph') or '')"],
                          capture_output=True, text=True, env=env, check=True).stdout == str(local / "codegraph")
    assert missing_dependencies(hub.Handler({"id": "graph", "on": ["prompt_submit"],
                                            "command": ["codegraph"]}), env["PATH"]) == []


def test_indexed_graph_handlers_execute_and_unindexed_skip(tmp_path, monkeypatch):
    home, local, _ = fixture(tmp_path)
    monkeypatch.setattr(hub.Path, "home", lambda: home)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".codegraph").mkdir()
    (repo / ".code-review-graph").mkdir()
    results = tmp_path / "executed.jsonl"
    for binary in ("codegraph", "code-review-graph"):
        executable(local / binary, "#!/usr/bin/python3\nimport json,os,sys\n"
                   "with open(os.environ['FAKE_EXECUTIONS'], 'a') as f: f.write(json.dumps([sys.argv[0],*sys.argv[1:]])+'\\n')\n")
    monkeypatch.setenv("PATH", f"{local}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_EXECUTIONS", str(results))
    monkeypatch.setenv("BB_HOOK_CLI", "claude")
    monkeypatch.setattr(concerns, "repository", lambda cwd=None: repo)
    monkeypatch.setattr(concerns, "disabled", lambda *args: False)
    for name in ("codegraph-prompt", "code-review-graph-status", "code-review-graph-update"):
        output = concerns.dispatch(name, {"cwd": str(repo)})
        assert output["_hook_hub"]["status"] == "succeeded", (name, output)
        assert output["_hook_hub"]["reason"] != "handler_binary_missing"
    assert [Path(row[0]).name for row in map(json.loads, results.read_text().splitlines())] == [
        "codegraph", "code-review-graph", "code-review-graph"]
    (repo / ".codegraph").rmdir()
    (repo / ".code-review-graph").rmdir()
    for name in ("codegraph-prompt", "code-review-graph-status", "code-review-graph-update"):
        assert concerns.dispatch(name, {"cwd": str(repo)})["_hook_hub"]["reason"] == "repository_not_indexed"
    assert len(results.read_text().splitlines()) == 3


def test_notification_rows_use_a_supported_executable_and_do_not_double_dispatch(tmp_path, monkeypatch):
    home, local, _ = fixture(tmp_path)
    monkeypatch.setattr(hub.Path, "home", lambda: home)
    monkeypatch.setenv("PATH", f"{local}:/usr/bin:/bin")
    monkeypatch.setattr(concerns, "disabled", lambda *args: False)
    (local / "claude-notify").symlink_to(tmp_path / "missing.js")
    wrapper = tmp_path / "concerns.py"
    wrapper.write_text("# fixture only\n")
    broken_row = hub.Handler({"id": "claude-notify", "on_native": ["Notification"],
                              "command": ["/usr/bin/python3", str(wrapper), "claude-notify"]})
    assert missing_dependencies(broken_row, hub.handler_path()) == ["claude-notify"]
    (local / "claude-notify").unlink()
    calls = tmp_path / "notifications.jsonl"
    executable(local / "claude-notify", "#!/usr/bin/python3\nimport os,sys\n"
               "with open(os.environ['FAKE_NOTIFICATIONS'], 'a') as f: f.write(os.environ['BB_HOOK_CLI']+'\\n')\n")
    monkeypatch.setenv("FAKE_NOTIFICATIONS", str(calls))
    import tomllib
    rows = tomllib.loads((Path(hub.__file__).parent / "handlers.toml").read_text())["handler"]
    assert [row["id"] for row in rows if row["id"] in {"claude-notify", "codex-turn-notify"}] == [
        "claude-notify", "codex-turn-notify"]
    for row, cli in (("claude-notify", "claude"), ("codex-turn-notify", "codex")):
        raw = next(r for r in rows if r["id"] == row).copy()
        raw["command"] = ["/usr/bin/python3", str(wrapper), "claude-notify"]
        handler = hub.Handler(raw)
        assert missing_dependencies(handler, hub.handler_path()) == []
        monkeypatch.setenv("BB_HOOK_CLI", cli)
        out = concerns.dispatch("claude-notify", {"session_id": "fixture"})
        assert out["_hook_hub"]["status"] == "succeeded"
    assert calls.read_text().splitlines() == ["claude", "codex"]


def test_preflight_warns_log_and_status_but_never_runs_or_blocks(tmp_path, monkeypatch):
    home, local, _ = fixture(tmp_path)
    monkeypatch.setattr(hub.Path, "home", lambda: home)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(hub, "LOG_PATH", tmp_path / "hub.log")
    monkeypatch.setattr(hub, "RECEIPT_PATH", tmp_path / "receipts.sqlite3")
    wrapper = tmp_path / "concerns.py"
    wrapper.write_text("raise RuntimeError('preflight must not run me')\n")
    disabled = hub.Handler({"id": "disabled", "on": ["prompt_submit"], "enabled": False,
                            "command": ["missing-disabled"]})
    missing = hub.Handler({"id": "codegraph-prompt", "on": ["prompt_submit"],
                           "command": ["/usr/bin/python3", str(wrapper), "codegraph-prompt"]})
    cfg = hub.Config()
    cfg.handlers = [disabled, missing]
    cfg._stamps = (cfg._stamp(hub.MASTER), *(cfg._stamp(p) for p in hub._overlay_registry()))
    cfg.preflight()
    assert cfg.missing == {"codegraph-prompt": ["codegraph"]}
    assert "handler codegraph-prompt: dependency missing: codegraph" in hub.LOG_PATH.read_text()
    assert "disabled" not in hub.LOG_PATH.read_text()
    server = hub.Server()
    server.cfg = cfg
    async def inspect():
        with mock.patch.object(server, "installed_inventory", return_value={"clis": []}):
            return await server.status()
    receipt = asyncio.run(inspect())
    assert {"handler_id": "codegraph-prompt", "command": "codegraph"} in receipt["dependency_warnings"]
    assert next(h for h in receipt["handlers"] if h["id"] == "codegraph-prompt")["state"] == "dependency_missing"
    assert next(h for h in receipt["handlers"] if h["id"] == "disabled")["state"] == "disabled"
    assert receipt["hub"]["state"] == "running"
    from facts import snapshot_projection
    projection = snapshot_projection(receipt)
    assert next(h for h in projection["handlers"] if h["id"] == "codegraph-prompt")["missing_commands"] == ["codegraph"]


def test_git_checkpoint_fallback_status_matches_real_daemon_dispatch():
    import tempfile
    from test_hub import HubHarness

    with tempfile.TemporaryDirectory(prefix="bb37-", dir=os.environ["TMPDIR"]) as short:
        base = Path(short)
        home = base / "home"
        fallback = home / "GitMark/bin/git-checkpoint"
        fallback.parent.mkdir(parents=True)
        marker = base / "checkpoint.txt"
        executable(fallback, "#!/bin/sh\nprintf '%s\\n' \"$1\" > \"$BB37_MARKER\"\n")
        subprocess.run(["/usr/bin/git", "init", "-q", str(base)], check=True)
        registry = f'''[[binding]]
cli = "claude"
native = "SessionEnd"
role = "session_end"

[[handler]]
id = "git-checkpoint"
mode = "async"
on = ["session_end"]
command = ["{sys.executable}", "{Path(concerns.__file__)}", "git-checkpoint"]
timeout_ms = 5000
'''
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "BB37_MARKER": str(marker),
               "HOOK_HUB_OVERLAY_DIR": str(base / "no-overlays")}
        with HubHarness(base, registry, env_extra=env) as daemon:
            status = daemon.request("claude", "SessionEnd", payload={}, env={})
            assert status["handled"] == ["git-checkpoint"], status
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert marker.read_text().strip() == str(base)
            # The socket status path invokes the same preflight as startup.
            import socket
            with socket.socket(socket.AF_UNIX) as conn:
                conn.settimeout(5)
                conn.connect(daemon.sock)
                conn.sendall(b'{"op":"status"}\n')
                conn.shutdown(socket.SHUT_WR)
                data = b""
                while b"\n" not in data:
                    data += conn.recv(65536)
            snapshot = json.loads(data.split(b"\n")[0])
            row = next(h for h in snapshot["handlers"] if h["id"] == "git-checkpoint")
            assert row["state"] == "configured" and row["missing_commands"] == []
            assert not any(w["handler_id"] == "git-checkpoint" for w in snapshot["dependency_warnings"])
            assert "dependency missing: git-checkpoint" not in daemon.log.read_text()


def test_git_checkpoint_missing_fallback_reports_attempted_path(tmp_path, monkeypatch):
    home, _, _ = fixture(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: home)
    from dependencies import git_checkpoint_command
    handler = hub.Handler({"id": "git-checkpoint", "on": ["session_end"],
                           "command": [sys.executable, str(Path(concerns.__file__)), "git-checkpoint"]})
    assert git_checkpoint_command("/usr/bin:/bin") == str(home / "code/GitMark/bin/git-checkpoint")
    assert missing_dependencies(handler, "/usr/bin:/bin") == [str(home / "code/GitMark/bin/git-checkpoint")]


def test_shebang_interpreter_and_dangling_link_are_reported(tmp_path):
    executable(tmp_path / "notify", "#!/usr/bin/env bb37-absent-interpreter\n")
    (tmp_path / "broken").symlink_to(tmp_path / "gone")
    for name in ("notify", "broken"):
        handler = hub.Handler({"id": name, "on": ["prompt_submit"], "command": [str(tmp_path / name)]})
        assert missing_dependencies(handler, "/usr/bin:/bin") == [str(tmp_path / name)]
