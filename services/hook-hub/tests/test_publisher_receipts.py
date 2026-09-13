"""Fail-open process exits must not masquerade as transport receipts."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

AGENT_HOOKS_DIR = Path(__file__).resolve().parents[2] / "agent-hooks"
sys.path.insert(0, str(AGENT_HOOKS_DIR))

from clients.base import ClientAdapter
from core.publisher import run


class Adapter(ClientAdapter):
    name = "test"
    source = "urn:test:hook"
    producer = "test-hooks"
    service = "test-hooks"
    actor_base = {"type": "agent_cli", "cli": "test", "agent_id": "test-hooks"}
    default_map = {"AfterTool": ("bloodbank.agent.tool.completed", "invocation")}

    def __init__(self, tmp):
        self._dir = tmp
        self.session_file = tmp / "session.json"

    @property
    def agent_dir(self):
        return self._dir

    def read_payload(self, argv):
        return {"session_id": "native-test-session", "prompt": "PRIVATE-CONTEXT"}

    def get_session_path(self, payload):
        return self.session_file

    def shape_data(self, session, ce_type, hook_name, payload, argv):
        return {"session_id": session.session_id}


@pytest.mark.parametrize("enabled,error,want,reason", [
    ("true", None, "succeeded", None),
    ("true", OSError("private transport endpoint"), "failed", "transport_failed"),
    ("false", None, "skipped", "publisher_disabled"),
])
def test_report_disambiguates_sent_failed_and_disabled(tmp_path, monkeypatch, enabled, error, want, reason):
    monkeypatch.setenv("BLOODBANK_ENABLED", enabled)
    monkeypatch.delenv("BLOODBANK_HOOK_STRICT", raising=False)
    report = {}
    with mock.patch("core.publisher._asm_observe"), mock.patch("core.publisher.nats_publish", side_effect=error) as publish:
        code = run(Adapter(tmp_path), ["publish.py", "AfterTool"], report=report)
    assert code == 0
    assert report["status"] == want
    assert report["reason"] == reason
    assert report["event_type"] == "bloodbank.agent.tool.completed"
    assert "PRIVATE-CONTEXT" not in json.dumps(report)
    assert "private transport endpoint" not in json.dumps(report)
    assert bool(publish.call_count) == (enabled == "true")
    if want == "succeeded":
        envelope = json.loads(publish.call_args.args[1])
        assert report["event_id"] == envelope["id"]
        assert envelope["correlationid"] == "native-test-session"


def test_unsupported_native_event_reports_skip(tmp_path):
    report = {}
    with mock.patch("core.publisher._asm_observe"), mock.patch("core.publisher.nats_publish") as publish:
        assert run(Adapter(tmp_path), ["publish.py", "unsupported"], report=report) == 0
    assert report["status"] == "skipped"
    assert report["reason"] == "unsupported_hook"
    publish.assert_not_called()
