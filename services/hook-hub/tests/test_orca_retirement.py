from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tomllib
from unittest import mock

import pytest

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))
import concerns
import cutover
import hub
import orca_retirement as retirement


@pytest.fixture
def config():
    result = hub.Config()
    with mock.patch.object(hub, "_overlay_registry", return_value=[SERVICE / "handlers.toml"]):
        result._load()
    return result


def setup_home(home, cli="claude", event="Stop", matcher=None):
    path = home / retirement.TARGETS[cli][0]
    path.parent.mkdir(parents=True, exist_ok=True)
    command = f"/bin/sh {home}/.orca/agent-hooks/{cli}-hook.sh"
    central = {"command": f"{home}/.agents/hooks/bb-hook --cli {cli} --native {event}"}
    foreign = {"command": "foreign-hook", "timeout": 17}
    group = {"hooks": [{"command": command}, central, foreign]}
    if matcher is not None:
        group["matcher"] = matcher
    doc = {"other": {"keep": True}, "hooks": {event: [group]}}
    path.write_text(json.dumps(doc))
    adapter = home / f".orca/agent-hooks/{cli}-hook.sh"
    adapter.parent.mkdir(parents=True, exist_ok=True)
    adapter.write_text("#!/bin/sh\nexit 0\n")
    adapter.chmod(0o700)
    return path, command, central, foreign


def planned(home, config, cli="claude", event="Stop", matcher=None, before=None):
    path, command, central, foreign = setup_home(home, cli, event, matcher)
    with mock.patch.dict(retirement.KNOWN, {cli: {retirement.fingerprint(command, home)}}):
        report, changes = retirement.plan(home, config, before)
    return path, central, foreign, report, changes


def test_preview_is_read_only_and_surgical_apply_is_idempotent(tmp_path, config):
    path, central, foreign, report, changes = planned(tmp_path, config)
    old = path.read_text()
    assert report[0]["action"] == "retire"
    assert path.read_text() == old
    retirement.apply_changes(changes, tmp_path / "recovery")
    result = json.loads(path.read_text())
    assert result["hooks"]["Stop"][0]["hooks"] == [central, foreign]
    assert result["other"] == {"keep": True}
    assert (tmp_path / "recovery/0-settings.json").read_text() == old
    assert (tmp_path / "recovery/0-settings.json").stat().st_mode & 0o777 == 0o600
    assert retirement.plan(tmp_path, config) == ([], {})


@pytest.mark.parametrize("event,reason", [("PreToolUse", "native_permission_owner"), ("Elicitation", "no_master_binding")])
def test_permission_and_unmapped_hooks_are_retained(tmp_path, config, event, reason):
    *_, report, changes = planned(tmp_path, config, event=event)
    assert report[0]["reason"] == reason
    assert changes == {}


def test_unknown_combined_operator_command_is_never_removed(tmp_path, config):
    path, *_ = setup_home(tmp_path)
    doc = json.loads(path.read_text())
    doc["hooks"]["Stop"][0]["hooks"][0]["command"] += "; foreign-side-effect"
    path.write_text(json.dumps(doc))
    report, changes = retirement.plan(tmp_path, config)
    assert report[0]["reason"] == "unknown_command_revision"
    assert changes == {}


@pytest.mark.parametrize("matcher,retire", [(".*", True), ("*", True), ("Write|Edit", False)])
def test_replacement_scope_must_cover_every_tool(tmp_path, config, matcher, retire):
    *_, report, changes = planned(tmp_path, config, event="PostToolUse", matcher=matcher)
    assert (report[0]["action"] == "retire") is retire


def test_missing_or_disabled_handler_blocks_retirement(tmp_path, config):
    config.handlers = [h for h in config.handlers if h.id != "orca-status"]
    *_, report, changes = planned(tmp_path, config)
    assert report[0]["reason"] == "handler_not_selected"
    assert not changes


def test_codex_uses_native_trust_not_json_presence(tmp_path, config):
    path, command, central, _ = setup_home(tmp_path, "codex")
    with mock.patch.dict(retirement.KNOWN, {"codex": {retirement.fingerprint(command, tmp_path)}}):
        report, changes = retirement.plan(tmp_path, config)
        assert report[0]["reason"] == "codex_native_trust_unproved"
        before = {"hooks": [{"sourcePath": str(path), "command": central["command"],
                             "eventName": "stop", "enabled": True, "trustStatus": "trusted"}]}
        report, changes = retirement.plan(tmp_path, config, before)
        assert report[0]["action"] == "retire"
        assert path in changes
        before["hooks"][0]["enabled"] = False
        assert retirement.plan(tmp_path, config, before)[0][0]["action"] == "retain"


def test_all_file_preflight_prevents_partial_apply(tmp_path, config):
    path, *_, changes = planned(tmp_path, config)
    second = tmp_path / "second.json"
    second.write_text("changed")
    changes[second] = ("old", "new")
    before = path.read_text()
    with pytest.raises(ValueError, match="config_changed_since_preview"):
        retirement.apply_changes(changes, tmp_path / "backups")
    assert path.read_text() == before
    assert not (tmp_path / "backups").exists()


def test_write_failure_restores_already_written_file(tmp_path, config):
    path, *_, changes = planned(tmp_path, config)
    second = tmp_path / "second.json"
    second.write_text("old")
    changes[second] = ("old", "new")
    before = path.read_text()
    original = cutover.write
    def fail(path_, text):
        if path_ == second and text == "new":
            raise OSError("injected")
        original(path_, text)
    with mock.patch.object(cutover, "write", side_effect=fail), pytest.raises(OSError):
        retirement.apply_changes(changes, tmp_path / "backups")
    assert path.read_text() == before


def test_symlink_destination_refused(tmp_path, config):
    path, command, *_ = setup_home(tmp_path)
    target = tmp_path / "source.json"
    path.rename(target)
    path.symlink_to(target)
    with mock.patch.dict(retirement.KNOWN, {"claude": {retirement.fingerprint(command, tmp_path)}}), pytest.raises(ValueError, match="symlink"):
        retirement.plan(tmp_path, config)


def test_kimi_removes_only_selected_table_and_preserves_other_bytes():
    text = '# user comment\nmodel="test"\n[[hooks]]\nevent="Stop"\ncommand="old"\n[[hooks]] # foreign\nevent="Stop"\ncommand="keep"\n[foreign]\nflag="off" # literal\n'
    updated = retirement.strip_kimi(text, {0})
    assert updated == text.replace('[[hooks]]\nevent="Stop"\ncommand="old"\n', "")
    assert tomllib.loads(updated)["foreign"]["flag"] == "off"


def test_kimi_indented_foreign_header_keeps_foreign_and_removes_exact_orca():
    raw = '  [[hooks]] # foreign\nevent="Stop"\ncommand="foreign-first"\n[[hooks]]\nevent="Stop"\ncommand="direct-orca"\n[[hooks]]\nevent="Stop"\ncommand="central"\n'
    updated = retirement.strip_kimi(raw, {1})
    assert [r["command"] for r in tomllib.loads(updated)["hooks"]] == ["foreign-first", "central"]
    assert '  [[hooks]] # foreign' in updated
    assert "direct-orca" not in updated


def test_kimi_unsupported_nested_tables_fail_closed():
    raw = '[[hooks]]\nevent="Stop"\ncommand="direct"\n[hooks.extra]\nflag=true\n'
    with pytest.raises(ValueError, match="nested_kimi_hook_table"):
        retirement.strip_kimi(raw, {0})


def test_source_ready_rejects_mode_and_missing_interpreter(tmp_path, monkeypatch):
    import shutil
    for rel, source in ((".agents/hooks/bloodbank/hooks.master.json", SERVICE.parent / "agent-hooks/hooks.master.json"),
                        (".agents/hooks/bb-hook", SERVICE / "client/bb-hook"),
                        *((f".agents/hooks/hub/{name}", SERVICE / name) for name in ("hub.py", "concerns.py", "handlers.toml", "orca_retirement.py"))):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    client = tmp_path / ".agents/hooks/bb-hook"
    client.chmod(0o644)
    with pytest.raises(ValueError, match="installed_client_not_executable"):
        retirement.source_ready(tmp_path)
    client.chmod(0o755)
    monkeypatch.setattr(retirement.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="installed_client_interpreter_missing"):
        retirement.source_ready(tmp_path)


def test_codex_native_disabled_direct_does_not_suppress_central(tmp_path, monkeypatch):
    path, _, _, _ = setup_home(tmp_path, "codex")
    raw = json.loads(path.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    snapshot = {"hooks": [{"sourcePath": str(path), "eventName": "stop", "command": raw,
                           "enabled": False, "trustStatus": "trusted"}]}
    assert retirement.direct_owner_present(tmp_path, "codex", "Stop", snapshot) is False
    snapshot["hooks"][0]["enabled"] = True
    assert retirement.direct_owner_present(tmp_path, "codex", "Stop", snapshot) is True
    snapshot["hooks"][0]["trustStatus"] = "untrusted"
    assert retirement.direct_owner_present(tmp_path, "codex", "Stop", snapshot) is False


def test_disabled_parent_and_nonmatching_direct_scope_do_not_suppress(tmp_path):
    path, *_ = setup_home(tmp_path)
    doc = json.loads(path.read_text())
    doc["hooks"]["Stop"][0]["enabled"] = False
    path.write_text(json.dumps(doc))
    assert not retirement.direct_owner_present(tmp_path, "claude", "Stop")
    doc["hooks"]["Stop"][0]["enabled"] = True
    doc["hooks"]["Stop"][0]["matcher"] = "Write|Edit"
    path.write_text(json.dumps(doc))
    assert not retirement.direct_owner_present(tmp_path, "claude", "Stop")


def test_retirement_refuses_live_hub_and_pending_queued_work(tmp_path, monkeypatch):
    import sqlite3
    path, *_ = setup_home(tmp_path)
    changes = {path: ("before", "after")}
    runtime = tmp_path / "runtime"
    sock = runtime / "33god/hook-hub.sock"
    sock.parent.mkdir(parents=True)
    sock.touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    with pytest.raises(ValueError, match="hub_socket_must_be_stopped_for_cutover"):
        retirement.assert_quiescent(tmp_path, changes)
    sock.unlink()
    dbpath = tmp_path / "state/33god/hook-hub/receipts.sqlite3"
    dbpath.parent.mkdir(parents=True)
    with sqlite3.connect(dbpath) as db:
        db.execute("CREATE TABLE executions (handler_id TEXT, status TEXT)")
        db.execute("INSERT INTO executions VALUES ('orca-status','selected')")
    with pytest.raises(ValueError, match="orca_work_not_drained"), mock.patch.object(retirement.Path, "exists", autospec=True, side_effect=lambda p: False if str(p) == f"/run/user/{os.getuid()}/33god/hook-hub.sock" else os.path.exists(p)):
        retirement.assert_quiescent(tmp_path, changes)


def test_captured_owner_survives_config_prune_before_queued_adapter_runs(tmp_path, monkeypatch):
    path, *_ = setup_home(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("ORCA_PANE_KEY", "pane")
    accepted = retirement.direct_owner_present(tmp_path, "claude", "Stop")
    assert accepted
    path.unlink()
    monkeypatch.setenv("BB_HOOK_ORCA_DIRECT_OWNER", "1" if accepted else "0")
    with mock.patch.object(concerns, "invoke") as run:
        assert concerns.orca({}, "claude", "Stop")["_hook_hub"]["reason"] == "orca_direct_owner_retained"
        run.assert_not_called()


def test_quiescence_requires_operator_attestation_even_when_no_process_detected(tmp_path, monkeypatch):
    # The CLI gate rejects the request before any config write even if the
    # process scan would see no native sessions.
    monkeypatch.setattr(retirement, "source_ready", lambda _: None)
    monkeypatch.setattr(retirement, "assert_quiescent", lambda *_: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["orca_retirement.py", "--apply", "--backup-dir", str(tmp_path / "backup")])
    with mock.patch.object(retirement, "plan", return_value=([{"action": "retire"}], {tmp_path / "hook.json": ("old", "new")})):
        with mock.patch.object(retirement, "apply_changes") as apply:
            assert retirement.main() == 1
            apply.assert_not_called()


def test_queued_hub_uses_acceptance_owner_after_native_config_pruned(tmp_path):
    import time
    from test_hub import HubHarness
    from receipts import ReceiptStore
    home = tmp_path / "home"
    path, *_ = setup_home(home)
    marker = tmp_path / "adapter-ran"
    adapter = home / ".orca/agent-hooks/claude-hook.sh"
    adapter.write_text(f"#!/bin/sh\ntouch {marker}\n")
    gate = tmp_path / "gate"
    started = tmp_path / "started"
    blocker = tmp_path / "blocker.py"
    blocker.write_text(f"import pathlib,time\npathlib.Path({str(started)!r}).touch()\np=pathlib.Path({str(gate)!r})\nend=time.monotonic()+5\nwhile not p.exists() and time.monotonic()<end: time.sleep(.01)\n")
    registry = (f'[[handler]]\nid="blocker"\nmode="async"\non_native=["Barrier"]\n'
                f'command=["{sys.executable}","{blocker}"]\ntimeout_ms=6000\n'
                f'[[handler]]\nid="orca-status"\nmode="async"\non_native=["Stop"]\n'
                f'clis=["claude"]\ncommand=["{sys.executable}","{SERVICE}/concerns.py","orca-status"]\ntimeout_ms=3000\n')
    run = tmp_path / "hub"
    run.mkdir()
    with mock.patch("test_hub._free_socket_path", return_value=str(run / "s")):
        with HubHarness(run, registry, {"HOME": str(home), "HOOK_HUB_ASYNC_SLOTS": "1",
                                        "HOOK_HUB_OVERLAY_DIR": str(home / "no-overlay")}) as daemon:
            daemon.request("claude", "Barrier")
            deadline = time.monotonic() + 3
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert started.exists()
            response = daemon.request("claude", "Stop", {"session_id": "queued"},
                                      {"ORCA_PANE_KEY": "fixture"})
            assert "orca-status" in response["handled"]
            path.unlink()
            gate.touch()
            deadline = time.monotonic() + 3
            store = ReceiptStore(run / "receipts.sqlite3")
            items = []
            while time.monotonic() < deadline:
                items = store.history(native="Stop")["items"]
                if items and items[0]["executions"][0]["status"] in {"skipped", "succeeded", "failed"}:
                    break
                time.sleep(.02)
            assert items[0]["executions"][0]["reason"] == "orca_direct_owner_retained"
            assert not marker.exists()


def test_same_session_orca_tasks_wait_for_previous(tmp_path):
    import asyncio
    from unittest.mock import patch
    async def check():
        with mock.patch.object(hub, "RECEIPT_PATH", tmp_path / "receipts.sqlite3"):
            server = hub.Server()
        started = asyncio.Event()
        release = asyncio.Event()
        order = []
        async def execute(handler, req, *_):
            if req["invocation_id"] == "1":
                started.set()
                await release.wait()
            order.append(req["invocation_id"])
        handler = hub.Handler({"id": "orca-status", "mode": "async", "on_native": ["Stop"],
                               "command": ["true"]})
        req = {"cli": "claude", "payload": {"session_id": "one-session"}, "native": "Stop",
               "invocation_id": "1"}
        with patch.object(server, "execute", side_effect=execute):
            server.spawn_async(handler, req, None, b"{}")
            await asyncio.wait_for(started.wait(), 2)
            server.spawn_async(handler, {**req, "invocation_id": "2"}, None, b"{}")
            await asyncio.sleep(.05)
            assert order == []
            release.set()
            await asyncio.wait_for(asyncio.gather(*server.background), 2)
        assert order == ["1", "2"]
    asyncio.run(check())


def test_general_cutover_no_longer_claims_orca():
    assert not cutover.owned("/bin/sh /home/me/.orca/agent-hooks/claude-hook.sh")


@pytest.mark.parametrize("cli", ["claude", "codex", "copilot", "kimi", "gemini", "antigravity"])
def test_every_supported_cli_selects_orca_and_keeps_sync_recall(config, cli):
    natives = [(native, binding) for (client, native), binding in config.bindings.items() if client == cli]
    for native, binding in natives:
        selected = config.select(binding.get("role"), native, {}, {}, cli=cli)
        assert any(h.id == "orca-status" for h in selected), (cli, native)
    recall = next(h for h in config.handlers if h.id == "hindsight-recall")
    assert recall.mode == "sync"


def test_adapter_receives_original_payload_event_and_pane_environment(tmp_path, monkeypatch):
    setup_home(tmp_path, "copilot")
    (tmp_path / ".copilot/hooks/orca.json").unlink()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("ORCA_PANE_KEY", "pane")
    monkeypatch.setenv("BB_HOOK_AGENT_PID", "123")
    monkeypatch.setenv("TMUX_PANE", "%8")
    with mock.patch.object(concerns, "invoke", return_value=concerns.result("succeeded", "handler_completed")) as run:
        response = concerns.orca({"private": "original"}, "copilot", "permissionRequest")
    assert response["_hook_hub"]["reason"] == "orca_adapter_completed"
    assert response["stdout"] == ""
    assert run.call_args.args[1] == {"private": "original", "hook_event_name": "PermissionRequest"}
    assert run.call_args.kwargs["environment"]["ORCA_COPILOT_HOOK_EVENT"] == "PermissionRequest"
    assert run.call_args.kwargs["environment"]["ORCA_HOOK_AGENT_PID"] == "123"
    assert run.call_args.kwargs["environment"]["TMUX_PANE"] == "%8"


@pytest.mark.parametrize("cli", ["claude", "antigravity"])
def test_permission_owner_is_not_double_dispatched(monkeypatch, cli):
    monkeypatch.setenv("ORCA_PANE_KEY", "pane")
    with mock.patch.object(concerns, "invoke") as run:
        assert concerns.orca({}, cli, "PreToolUse")["_hook_hub"]["reason"] == "orca_direct_owner_retained"
        run.assert_not_called()


def test_installed_source_must_match_candidate(tmp_path):
    with pytest.raises(OSError):
        retirement.source_ready(tmp_path)


def test_client_forwards_orca_identity_without_generic_credentials(monkeypatch):
    namespace = {"__name__": "notmain"}
    client = SERVICE / "client/bb-hook"
    exec(compile(client.read_text(), str(client), "exec"), namespace)
    values = {"ORCA_PANE_KEY": "pane", "ORCA_AGENT_LAUNCH_TOKEN": "fixture-launch",
              "ORCA_AGENT_HOOK_ROOT": "/fixture", "TMUX_PANE": "%9", "CLAUDE_JOB_DIR": "/fixture/job",
              "ORCA_AGENT_HOOK_TOKEN": "do-not-forward", "HINDSIGHT_API_KEY": "do-not-forward"}
    captured = namespace["captured_env"]
    assert callable(captured)
    with mock.patch.dict(os.environ, values, clear=True):
        observed = captured()
    assert {k: observed[k] for k in values if k in observed} == {
        k: v for k, v in values.items() if k not in {"ORCA_AGENT_HOOK_TOKEN", "HINDSIGHT_API_KEY"}}


def test_shell_chains_and_disabled_native_replacement_are_not_coverage(tmp_path, config):
    path, command, central, _ = setup_home(tmp_path)
    doc = json.loads(path.read_text())
    doc["hooks"]["Stop"][0]["hooks"][1]["command"] += "; false"
    path.write_text(json.dumps(doc))
    with mock.patch.dict(retirement.KNOWN, {"claude": {retirement.fingerprint(command, tmp_path)}}):
        assert retirement.plan(tmp_path, config)[0][0]["reason"] == "no_unconditional_native_replacement"


def test_direct_owner_handoff_prevents_double_dispatch(tmp_path, config, monkeypatch):
    path, *_, changes = planned(tmp_path, config)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("ORCA_PANE_KEY", "pane")
    with mock.patch.object(concerns, "invoke", return_value=concerns.result("succeeded", "ok")) as run:
        assert concerns.orca({}, "claude", "Stop")["_hook_hub"]["reason"] == "orca_direct_owner_retained"
        run.assert_not_called()
        retirement.apply_changes(changes, tmp_path / "backups")
        assert concerns.orca({}, "claude", "Stop")["_hook_hub"]["reason"] == "orca_adapter_completed"
        assert run.call_count == 1


@pytest.mark.parametrize("cli,native,event", [("claude", "Stop", "Stop"), ("codex", "Interrupt", "Interrupt"),
    ("copilot", "permissionRequest", "PermissionRequest"), ("kimi", "Stop", "Stop"),
    ("gemini", "AfterAgent", "AfterAgent"), ("antigravity", "PostInvocation", "PostInvocation")])
def test_real_installed_adapter_contract_against_private_http(tmp_path, monkeypatch, cli, native, event):
    # Exercise actual operator adapters, but never their actual endpoint, spool,
    # tokens or home. These files are read-only dependencies, not test output.
    import http.server
    import queue
    import shutil
    import threading
    import urllib.parse
    source = Path.home() / f".orca/agent-hooks/{cli}-hook.sh"
    if not source.is_file():
        pytest.skip("operator Orca adapter not installed")
    adapter = tmp_path / f".orca/agent-hooks/{cli}-hook.sh"
    adapter.parent.mkdir(parents=True)
    shutil.copy2(source, adapter)
    incoming = queue.Queue()
    class Endpoint(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            incoming.put((self.path, dict(self.headers), urllib.parse.parse_qs(body.decode())))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
        def log_message(self, format, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = tmp_path / "endpoint.sh"
    endpoint.write_text(f"export ORCA_AGENT_HOOK_PORT={server.server_port}\nexport ORCA_AGENT_HOOK_TOKEN=fixture-secret\n")
    environment = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "ORCA_PANE_KEY": "fixture-pane",
                   "ORCA_TAB_ID": "fixture-tab", "ORCA_WORKTREE_ID": "fixture-tree",
                   "ORCA_AGENT_LAUNCH_TOKEN": "fixture-launch", "ORCA_AGENT_HOOK_ENDPOINT": str(endpoint),
                   "BB_HOOK_AGENT_PID": str(os.getpid()), "TMUX": "/fixture,1,2", "TMUX_PANE": "%42"}
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    try:
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(retirement, "direct_owner_present", return_value=False):
            response = concerns.orca({"session_id": "fixture-session", "custom": "kept"}, cli, native)
        path, headers, form = incoming.get(timeout=3)
        assert path == f"/hook/{cli}"
        assert headers["X-Orca-Agent-Hook-Token"] == "fixture-secret"
        assert form["paneKey"] == ["fixture-pane"]
        assert form["launchToken"] == ["fixture-launch"]
        assert json.loads(form["payload"][0]) == {"session_id": "fixture-session", "custom": "kept", "hook_event_name": event}
        if cli == "copilot":
            assert form["hookEventName"] == [event]
        if cli == "antigravity":
            assert form["hook_event_name"] == [event]
            assert form["tmuxPane"] == ["%42 /fixture"]
        assert response["_hook_hub"]["reason"] == "orca_adapter_completed"
        assert response["stdout"] == ""
        # The shell fallback still spools when the endpoint is unavailable.
        server.shutdown()
        server.server_close()
        endpoint.write_text("export ORCA_AGENT_HOOK_PORT=1\nexport ORCA_AGENT_HOOK_TOKEN=fixture-secret\n")
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(retirement, "direct_owner_present", return_value=False):
            response = concerns.orca({"session_id": "offline-session"}, cli, native)
        spool = list((tmp_path / "spool").glob("*.jsonl"))
        assert len(spool) == 1
        recorded = [json.loads(line) for line in spool[0].read_text().splitlines() if line.strip()]
        assert recorded[-1]["payload"]["session_id"] == "offline-session"
        assert response["_hook_hub"]["reason"] == "orca_adapter_completed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
