from __future__ import annotations

import copy
import importlib.util
import json
import tomllib
import sys
import subprocess
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clients import get_adapter
from codex_native import trust_edits
from core.event_map import resolve_map
from core.session import SessionState
from health.installed_inventory import collect_installed_inventory, native_rows
import sync


def test_codex_publisher_escapes_foreign_write_matcher_without_changing_siblings():
    master = sync.load_master()
    agent = master["agents"]["codex"]
    generated = sync.render_config(agent, master["lifecycle"], sync.load_lock())
    foreign = {"type": "command", "command": "/foreign/retain.sh", "timeout": 5}
    live = {"other": {"keep": True}, "hooks": {"PostToolUse": [{
        "matcher": "Write|Edit|MultiEdit", "condition": {"keep": True}, "hooks": [
            foreign, {"type": "command", "command": "/old/bloodbank/publish.py --client codex --hook PostToolUse", "timeout": 3000}
        ]}]}}
    merged = sync._merge_hooks(live, generated["hooks"], sync._publisher_markers("codex", agent))
    groups = merged["hooks"]["PostToolUse"]
    assert groups[0] == {"matcher": "Write|Edit|MultiEdit", "condition": {"keep": True}, "hooks": [foreign]}
    assert groups[1]["matcher"] == ".*"
    assert len(groups[1]["hooks"]) == 1
    assert groups[1]["hooks"][0]["timeout"] == 4
    once = copy.deepcopy(merged)
    assert sync._merge_hooks(merged, generated["hooks"], sync._publisher_markers("codex", agent)) == once


def test_each_supported_native_event_has_one_hub_command_and_correct_timeout_unit():
    master = sync.load_master()
    for name, agent in master["agents"].items():
        if agent.get("support_status") != "supported" or agent["dialect"] == "opencode_plugin":
            continue
        generated = sync.render_config(agent, master["lifecycle"], sync.load_lock())
        rows = native_rows(generated, agent["dialect"])
        assert len(rows) == len(agent["bindings"]), name
        for binding in agent["bindings"]:
            selected = [r for r in rows if r["native"] == binding["native"]]
            assert len(selected) == 1
            row = selected[0]
            assert f"bb-hook --cli {name} --native {binding['native']}" in row["command"]
            assert "echo" not in row["command"] and "cat |" not in row["command"]
            if binding["role"] in {"prompt_submit", "session_start"}:
                assert "--deadline 15" in row["command"]
                assert row["timeout"] == (16000 if name == "gemini" else 16)
            else:
                assert row["timeout"] <= (4000 if name == "gemini" else 4)


def test_kimi_toml_replaces_only_managed_sections_and_preserves_foreign_values():
    raw = 'model = "keep"\n\n[[hooks]]\nevent = "Stop"\ncommand = "foreign"\ntimeout = 8\n\n[[hooks]]\nevent = "Stop"\ncommand = "/old/bloodbank/publish.py Stop"\n\n[mcp_servers.local]\ncommand = "keep-me"\n'
    rows = [{"event": "Stop", "command": "/new/bb-hook --cli kimi --native Stop", "timeout": 4}]
    merged = sync._merge_kimi_toml(raw, rows, ["bb-hook", "bloodbank/publish.py"])
    parsed = tomllib.loads(merged)
    assert parsed["model"] == "keep"
    assert parsed["mcp_servers"]["local"]["command"] == "keep-me"
    assert parsed["hooks"][0]["command"] == "foreign"
    assert parsed["hooks"][1] == rows[0]
    assert sync._merge_kimi_toml(merged, rows, ["bb-hook", "bloodbank/publish.py"]) == merged


def test_discovery_prefers_current_profiles_and_keeps_real_legacy_process_home(tmp_path):
    profiles = tmp_path / "profiles"
    for name in ("live-pm", "old.bak-123"):
        (profiles / name).mkdir(parents=True)
        (profiles / name / "config.yaml").write_text("hooks_auto_accept: true\n")
    stale = tmp_path / "old-runtime"
    stale.mkdir()
    (stale / "config.yaml").write_text("{}")
    registry = tmp_path / "registry.yaml"
    registry.write_text(f"agents:\n  old:\n    role_dir: {tmp_path}\n")
    active = tmp_path / "active-legacy"
    active.mkdir()
    (active / "config.yaml").write_text("{}")
    proc = tmp_path / "proc/321"
    proc.mkdir(parents=True)
    (proc / "environ").write_bytes(f"HERMES_HOME={active}\0IGNORED=value\0".encode())
    agent = {"profiles_dir": str(profiles), "fleet_registry": str(registry), "runtime_subdir": "old-runtime"}
    found = sync.discover_hermes_configs(agent, proc_root=proc.parent)
    assert {p for _, p, _ in found} == {profiles / "live-pm/config.yaml", active / "config.yaml"}


def test_empty_installed_hook_config_fails_expected_coverage(tmp_path):
    master = copy.deepcopy(sync.load_master())
    master["agents"] = {"codex": master["agents"]["codex"]}
    live = tmp_path / "hooks.json"
    live.write_text('{"hooks": {}}')
    master["agents"]["codex"]["live_target"] = str(live)
    report = collect_installed_inventory(master, probe_native=False)
    cli = report["clis"][0]
    assert report["status"] == "drift"
    assert all(row["expected_count"] == 1 and row["actual_hub_count"] == 0 and row["status"] == "missing" for row in cli["natives"])


def test_trust_migration_preserves_foreign_approval_and_disabled_state_by_hash():
    source = Path("/tmp/.codex/hooks.json")
    def hook(key, digest, command):
        return {"key": key, "sourcePath": str(source), "eventName": "postToolUse", "currentHash": digest, "command": command}
    before = {"hooks": [hook("old:0", "foreign-hash", "foreign"), hook("old:1", "publisher-hash", "/old/publish.py")],
              "states": {"old:0": {"trusted_hash": "foreign-hash", "enabled": False}}}
    after = [hook("new:0", "foreign-hash", "foreign"), hook("new:1", "hub-hash", "/h/bb-hook --cli codex --native PostToolUse"), hook("new:2", "new-foreign", "new foreign")]
    edits = trust_edits(before, after, source)
    assert len(edits) == 2
    assert edits[0]["value"] == before["states"]["old:0"]
    assert edits[1]["value"] == {"trusted_hash": "hub-hash", "enabled": True}


def test_native_sessions_remain_separate_and_keep_their_native_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    adapter = get_adapter("codex")
    left, right = {"session_id": "session/left"}, {"session_id": "session/right"}
    a = SessionState(adapter.get_session_path(left), native_id=adapter.native_session_id(left))
    b = SessionState(adapter.get_session_path(right), native_id=adapter.native_session_id(right))
    a.record_event("a1")
    b.record_event("b1")
    a.bump_tool("Bash")
    assert a.session_id == "session/left" and b.session_id == "session/right"
    assert a.path != b.path
    assert SessionState(a.path).last_event_id == "a1"
    assert SessionState(b.path).last_event_id == "b1"
    assert SessionState(b.path).tools_used == {}


def test_turn_completion_is_not_session_closure_and_new_adapters_keep_identity(tmp_path):
    for name, turn, ended in [("claude", "Stop", "SessionEnd"), ("codex", "Stop", "SessionEnd"), ("hermes", "on_session_end", "on_session_finalize"), ("kimi", "Stop", "SessionEnd"), ("gemini", "AfterAgent", "SessionEnd"), ("opencode", "session.idle", "session.deleted")]:
        adapter = get_adapter(name)
        mapping = resolve_map(adapter.agent_dir, adapter.default_map)
        assert mapping[turn][0] == "bloodbank.conversation.turn.completed"
        assert mapping[ended][0] == "bloodbank.agent.session.ended"
        assert adapter.get_actor({})["cli"] == name
        session = SessionState(tmp_path / f"{name}.json", native_id="native-session")
        data = adapter.shape_data(session, mapping[turn][0], turn, {"session_id": "native-session"}, ["publish.py", turn])
        assert data["thread_id"] == "native-session"
        assert data["outcome"] == "completed"


def test_opencode_native_bridge_keeps_session_identity_context_and_failed_tool_once(tmp_path):
    helper = tmp_path / "bb-hook"
    receipt = tmp_path / "calls.jsonl"
    helper.write_text("#!/usr/bin/env python3\nimport json,sys,os\np=json.load(sys.stdin)\nwith open(os.environ['BB_TEST_RECEIPTS'],'a') as f:f.write(json.dumps({'args':sys.argv[1:],'payload':p})+'\\n')\nif p['hook_event_name']=='chat.message':print(json.dumps({'hookSpecificOutput':{'additionalContext':'fixture context'}}))\n")
    helper.chmod(0o755)
    plugin = Path(__file__).resolve().parents[1] / "opencode/hook-hub.js"
    script = f"""
import {{ BloodbankHookHub }} from {json.dumps(plugin.as_uri())};
const hooks = await BloodbankHookHub({{ directory: {json.dumps(str(tmp_path))} }});
await hooks.event({{event:{{type:'session.created',properties:{{info:{{id:'session-A'}}}}}}}});
const output = {{message:{{id:'turn-A'}},parts:[{{type:'text',text:'hello'}}]}};
await hooks['chat.message']({{sessionID:'session-A',messageID:'turn-A',model:{{modelID:'model',providerID:'provider'}}}},output);
const input = {{sessionID:'session-A',callID:'call-1',tool:'bash'}};
await hooks['tool.execute.before'](input,{{args:{{command:'false'}}}});
await hooks.event({{event:{{type:'message.part.updated',properties:{{part:{{type:'tool',sessionID:'session-A',callID:'call-1',state:{{status:'error',error:'fixture failure'}}}}}}}}}});
await hooks['tool.execute.after'](input,{{output:'late duplicated callback'}});
await hooks.event({{event:{{type:'session.idle',properties:{{sessionID:'session-A'}}}}}});
await hooks.event({{event:{{type:'session.deleted',properties:{{info:{{id:'session-A'}}}}}}}});
console.log(JSON.stringify(output.parts));
"""
    env = {**os.environ, "BB_HOOK_COMMAND": str(helper), "BB_TEST_RECEIPTS": str(receipt)}
    result = subprocess.run(["node", "--input-type=module", "-e", script], env=env,
                            capture_output=True, text=True, timeout=10, check=True)
    parts = json.loads(result.stdout)
    assert parts[-1]["text"] == "fixture context"
    calls = [json.loads(line) for line in receipt.read_text().splitlines()]
    assert [call["payload"]["hook_event_name"] for call in calls] == [
        "session.created", "chat.message", "tool.execute.before", "tool.execute.after", "session.idle", "session.deleted"]
    assert all(call["payload"]["session_id"] == "session-A" for call in calls)
    assert calls[3]["payload"]["is_error"] is True
    assert calls[3]["payload"]["tool_call_id"] == "call-1"
    assert calls[1]["args"][-2:] == ["--deadline", "15"]
