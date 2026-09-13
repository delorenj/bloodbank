"""Already-running CLIs can keep their cached command across native cutover."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

from test_hub import CLIENT, HUB_DIR, HubHarness, echo_handler
from test_receipts import request
from receipts import ReceiptStore

AGENT_DIR = HUB_DIR.parent / "agent-hooks"
sys.path.insert(0, str(AGENT_DIR))
from clients.hermes import HermesAdapter
from core.publisher import _forward_legacy_hook


def owner_env(tmp_path):
    ownership = tmp_path / "ownership.json"
    ownership.write_text(json.dumps({"version": 1, "clis": ["hermes"], "handler_ids": ["orca-status"]}))
    env = {**os.environ, "BB_HOOK_OWNERSHIP": str(ownership), "BLOODBANK_ENABLED": "false"}
    env.pop("BB_HOOK_HUB", None)
    return env


def test_cached_hermes_publisher_forwards_context_and_original_payload(tmp_path):
    env = owner_env(tmp_path)
    script = tmp_path / "context.py"
    script.write_text("import json,sys\np=json.load(sys.stdin)\nprint('context:'+p['session_id'])\n")
    reg = f'[[handler]]\nid="context"\nmode="sync"\non=["prompt_submit"]\ncommand=["{sys.executable}","{script}"]\ntimeout_ms=1000\n'
    with HubHarness(tmp_path, reg) as hub:
        proc = subprocess.run([sys.executable, str(AGENT_DIR / "hermes/publish.py"), "pre_llm_call"],
                              input=json.dumps({"session_id": "cached-hermes", "user_message": "private"}).encode(),
                              capture_output=True, env={**env, "BB_HOOK_SOCKET": hub.sock}, timeout=5)
        assert proc.returncode == 0
        assert proc.stdout.decode().strip() == "context:cached-hermes"
        status = ReceiptStore(tmp_path / "receipts.sqlite3").summary()
        row = next(row for row in status["native_activity"] if row["cli"] == "hermes")
        assert row["native"] == "pre_llm_call"


def test_cached_publisher_and_new_native_command_share_invocation_identity(tmp_path):
    env = owner_env(tmp_path)
    reg = echo_handler("one", "OK", on='["post_tool"]')
    payload = {"session_id": "cached-hermes", "tool_call_id": "tool-one", "tool_name": "terminal"}
    with HubHarness(tmp_path, reg) as hub:
        old = subprocess.run([sys.executable, str(AGENT_DIR / "hermes/publish.py"), "post_tool_call"],
                             input=json.dumps(payload).encode(), capture_output=True,
                             env={**env, "BB_HOOK_SOCKET": hub.sock}, timeout=5)
        assert old.stdout.decode().strip() == "OK"
        new = request(hub, cli="hermes", native="post_tool_call", payload=payload)
        assert new["deduplicated"]
        assert new["handled"] == ["one"]


def test_hub_publisher_child_bypasses_forwarding_without_reading_input(monkeypatch):
    monkeypatch.setenv("BB_HOOK_HUB", "off")
    adapter = HermesAdapter()
    with mock.patch.object(adapter, "read_payload") as read, mock.patch("core.publisher.subprocess.run") as run:
        assert _forward_legacy_hook(adapter, ["publish.py", "post_tool_call"]) is None
        read.assert_not_called()
        run.assert_not_called()


@pytest.mark.parametrize("manifest", [None, "malformed", '{"version":1,"clis":["codex"]}'])
def test_before_ownership_legacy_pipeline_keeps_its_input(tmp_path, monkeypatch, manifest):
    path = tmp_path / "ownership.json"
    if manifest is not None:
        path.write_text(manifest)
    monkeypatch.setenv("BB_HOOK_OWNERSHIP", str(path))
    monkeypatch.delenv("BB_HOOK_HUB", raising=False)
    adapter = HermesAdapter()
    with mock.patch.object(adapter, "read_payload") as read:
        assert _forward_legacy_hook(adapter, ["publish.py", "post_tool_call"]) is None
        read.assert_not_called()


def test_owned_missing_client_fails_open_without_direct_publication(tmp_path, monkeypatch):
    for key, value in owner_env(tmp_path).items():
        if key == "BB_HOOK_OWNERSHIP":
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("BB_HOOK_HUB", raising=False)
    with mock.patch("core.publisher.Path.is_file", return_value=False), mock.patch("core.publisher.subprocess.run") as run:
        assert _forward_legacy_hook(HermesAdapter(), ["publish.py", "post_tool_call"]) == 0
        run.assert_not_called()
