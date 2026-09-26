"""Execution proof through the real daemon; no live hooks or broker traffic."""
from __future__ import annotations

import concurrent.futures
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

from test_hub import CLIENT, HUB_DIR, HubHarness, echo_handler

sys.path.insert(0, str(HUB_DIR))
from receipts import ReceiptStore, invocation_identity, native_session_id
from hub import compose_stdout


def request(hub, *, iid=None, payload=None, native="UserPromptSubmit", **extra):
    req = {"v": 1, "cli": "claude", "native": native,
           "cwd": str(hub.tmp), "payload": payload or {}, **extra}
    if iid:
        req["invocation_id"] = iid
    return json.loads(hub.send_raw(json.dumps(req).encode()))


def detail(tmp, iid):
    return ReceiptStore(tmp / "receipts.sqlite3").detail(iid)


def test_duplicate_delivery_runs_one_handler_and_replays_sync_output(tmp_path):
    marker = tmp_path / "calls"
    script = tmp_path / "handler.py"
    script.write_text(f"from pathlib import Path\nimport time\np=Path({str(marker)!r})\np.write_text(p.read_text()+'x' if p.exists() else 'x')\ntime.sleep(.15)\nprint('CONTEXT')\n")
    registry = f'[[handler]]\nid="one"\nmode="sync"\non=["prompt_submit"]\ncommand=["{sys.executable}","{script}"]\ntimeout_ms=1000\n'
    with HubHarness(tmp_path, registry) as hub:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: request(hub, iid="native-event-123"), range(2)))
        assert marker.read_text() == "x"
        assert {r["stdout"] for r in replies} == {"CONTEXT"}
        assert sum(r["deduplicated"] for r in replies) == 1
        receipt = detail(tmp_path, replies[0]["invocation_id"])
        assert receipt["deduplicated"] == 1
        assert [r["status"] for r in receipt["timeline"] if r["handler_id"]] == ["selected", "started", "succeeded"]


def test_repeated_prompt_without_native_id_is_not_deduplicated(tmp_path):
    with HubHarness(tmp_path, echo_handler("context", "same")) as hub:
        a = request(hub, payload={"prompt": "same user text"})
        b = request(hub, payload={"prompt": "same user text"})
        assert a["invocation_id"] != b["invocation_id"]
        assert not b["deduplicated"]
        assert detail(tmp_path, a["invocation_id"])["identity_kind"] == "generated"


def test_tool_identity_is_scoped_to_native_event_and_session():
    req = {"cli": "codex", "native": "PostToolUse", "payload": {"session_id": "s1", "tool_use_id": "t1"}}
    original, kind = invocation_identity(req)
    assert kind == "tool_call"
    assert invocation_identity(req)[0] == original
    assert invocation_identity({**req, "native": "PreToolUse"})[0] != original
    assert invocation_identity({**req, "payload": {"session_id": "s2", "tool_use_id": "t1"}})[0] != original


def test_nested_hermes_ids_match_adapter_session_and_deduplicate_like_flat_payload():
    payload = {"session_id": "hermes-session", "tool_call_id": "one-tool"}
    flat = {"cli": "hermes", "native": "post_tool_call", "payload": payload}
    nested = {**flat, "payload": {"extra": payload}}
    assert native_session_id(nested) == "hermes-session"
    assert invocation_identity(nested) == invocation_identity(flat)
    assert invocation_identity(nested)[1] == "tool_call"


def test_long_native_ids_do_not_deduplicate_on_a_shared_prefix():
    req = {"cli": "hermes", "native": "post_tool_call", "payload": {"session_id": "s", "tool_call_id": "a" * 256 + "one"}}
    other = {**req, "payload": {"session_id": "s", "tool_call_id": "a" * 256 + "two"}}
    assert invocation_identity(req)[0] != invocation_identity(other)[0]


def test_failed_handler_receipt_does_not_store_input_or_stderr(tmp_path):
    secret = "DO-NOT-LOG-PRIVATE-HOOK-CONTENT"
    script = tmp_path / "handler.py"
    script.write_text("import sys\nsys.stderr.write(sys.stdin.read())\nsys.exit(1)\n")
    registry = f'[[handler]]\nid="failed"\nmode="sync"\non=["prompt_submit"]\ncommand=["{sys.executable}","{script}"]\ntimeout_ms=1000\n'
    with HubHarness(tmp_path, registry) as hub:
        reply = request(hub, payload={"prompt": secret})
        receipt = detail(tmp_path, reply["invocation_id"])
        assert receipt["status"] == "failed"
        assert receipt["executions"][0]["status"] == "failed"
        assert receipt["executions"][0]["reason"] == "nonzero_exit"
        assert reply["exit_code"] == 0
        assert secret not in json.dumps(receipt)
        for path in tmp_path.glob("receipts.sqlite3*"):
            assert secret.encode() not in path.read_bytes()
        assert secret not in hub.log.read_text()


def test_native_blocking_exit_and_structured_context_survive(tmp_path):
    script = tmp_path / "handler.py"
    script.write_text("import json,sys\nprint(json.dumps({'_hook_hub': {'status':'succeeded','exit_code':2},'stdout': json.dumps({'decision':'block','reason':'policy'})}))\nsys.exit(2)\n")
    registry = f'[[handler]]\nid="policy"\nmode="sync"\non=["prompt_submit"]\ncommand=["{sys.executable}","{script}"]\ntimeout_ms=1000\n'
    with HubHarness(tmp_path, registry) as hub:
        proc = subprocess.run([str(CLIENT), "--cli", "claude", "--native", "UserPromptSubmit"],
                              input=b"{}", capture_output=True,
                              env={**os.environ, "BB_HOOK_SOCKET": hub.sock}, timeout=5)
        assert proc.returncode == 2
        assert json.loads(proc.stdout) == {"decision": "block", "reason": "policy"}
        assert b"_hook_hub" not in proc.stdout


def test_json_composition_cannot_erase_an_earlier_denial():
    value = json.loads(compose_stdout([
        json.dumps({"hookSpecificOutput": {"permissionDecision": "deny", "additionalContext": "first"}}),
        json.dumps({"hookSpecificOutput": {"permissionDecision": "allow", "additionalContext": "second"}}),
    ]))
    assert value["hookSpecificOutput"] == {"permissionDecision": "deny", "additionalContext": "first\n\nsecond"}


def test_missing_context_is_a_recorded_skip_not_a_failure(tmp_path):
    reg = echo_handler("pane", "bad", extra='require_env=["ZELLIJ_PANE_ID"]')
    with HubHarness(tmp_path, reg) as hub:
        reply = request(hub)
        row = detail(tmp_path, reply["invocation_id"])["executions"][0]
        assert row["status"] == "skipped"
        assert row["reason"] == "missing_environment"
        assert row["started_at"] is None


def test_restarting_hub_does_not_replay_claimed_side_effect(tmp_path):
    reg = echo_handler("one", "FIRST")
    with HubHarness(tmp_path, reg) as hub:
        first = request(hub, iid="stable")
    with HubHarness(tmp_path, echo_handler("one", "SHOULD-NOT-RUN")) as hub:
        second = request(hub, iid="stable")
        assert second["invocation_id"] == first["invocation_id"]
        assert second["deduplicated"]
        assert second["stdout"] == ""  # context is intentionally not persisted
        assert len(detail(tmp_path, first["invocation_id"])["executions"]) == 1


def test_recovery_reports_interrupted_handlers_without_replay(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    row = {"invocation_id": "i", "cli": "claude", "native": "Stop", "role": "turn_completed",
           "event_type": None, "session_id": "s", "identity_kind": "provided", "received_at": "2026-09-13T00:00:00Z"}
    assert store.claim(row)
    store.select("i", "retention", "async")
    store.update("i", "retention", "started")
    store.recover()
    value = store.detail("i")
    assert value["status"] == "failed"
    assert value["executions"][0]["reason"] == "hub_restarted"
    assert not store.claim(row)


def test_read_api_reports_registry_separately_from_observation_and_paginates(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with HubHarness(tmp_path, echo_handler("context", "OK"), {"HOOK_HUB_HTTP_PORT": str(port)}) as hub:
        one = request(hub)
        request(hub)
        with urlopen(base + "/v1/hooks/status", timeout=10) as response:
            status = json.load(response)
        assert status["schema_version"] == 1
        assert status["observed_since"]
        unseen = next(b for b in status["bindings"] if b["cli"] == "copilot")
        assert unseen["observed_state"] == "unobserved"
        planned = [b for b in status["bindings"] if b["support_status"] != "supported"]
        assert planned
        assert all(b["state"] == "unsupported" and not b["configured"] and not b["handler_ids"] for b in planned)
        with urlopen(base + "/v1/hooks/invocations?cli=claude&handler=context&limit=1", timeout=3) as response:
            page = json.load(response)
        assert page["total"] == 2 and len(page["items"]) == 1
        assert page["next_offset"] == 1
        with urlopen(base + "/v1/hooks/invocations/" + one["invocation_id"], timeout=3) as response:
            item = json.load(response)
        assert item["invocation"]["executions"][0]["status"] == "succeeded"


def test_ordinary_tool_hook_does_not_wait_for_async_work(tmp_path):
    reg = '[[handler]]\nid="slow"\nmode="async"\non=["post_tool"]\ncommand=["/usr/bin/sleep","1"]\ntimeout_ms=2000\n'
    with HubHarness(tmp_path, reg) as hub:
        started = time.monotonic()
        reply = request(hub, native="PostToolUse")
        assert time.monotonic() - started < .5
        assert reply["handled"] == ["slow"]


def test_session_end_waits_for_prior_candidate_writes_only_in_its_session(tmp_path):
    script = tmp_path / "retention.py"
    script.write_text(f"import json,sys,time\nfrom pathlib import Path\np=json.load(sys.stdin)\ns=p['session_id']\nroot=Path({str(tmp_path)!r})\nif sys.argv[1]=='candidate':\n time.sleep(.5)\n (root/(s+'.candidate')).write_text('retained')\nelse:\n source=root/(s+'.candidate')\n (root/(s+'.summary')).write_text(source.read_text() if source.exists() else 'empty')\n")
    reg = f'''[[handler]]
id="candidate"
mode="async"
on=["post_tool"]
command=["{sys.executable}","{script}","candidate"]
timeout_ms=2000
[[handler]]
id="end"
mode="async"
on=["session_end"]
after=["candidate"]
command=["{sys.executable}","{script}","end"]
timeout_ms=2000
'''
    with HubHarness(tmp_path, reg) as hub:
        request(hub, native="PostToolUse", payload={"session_id": "one"})
        began = time.monotonic()
        request(hub, native="SessionEnd", payload={"session_id": "one"})
        request(hub, native="SessionEnd", payload={"session_id": "other"})
        assert time.monotonic() - began < .3
        deadline = time.monotonic() + .4
        while not (tmp_path / "other.summary").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (tmp_path / "other.summary").read_text() == "empty"
        assert not (tmp_path / "one.summary").exists()
        deadline = time.monotonic() + 3
        while not (tmp_path / "one.summary").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (tmp_path / "one.summary").read_text() == "retained"


def test_central_publisher_emits_one_event_for_duplicate_native_delivery(tmp_path):
    captured = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(3)
    port = listener.getsockname()[1]

    def broker():
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(3)
                conn.sendall(b'INFO {"server_id":"isolated-hook-test"}\r\n')
                frame = bytearray()
                while b"PING\r\n" not in frame:
                    frame.extend(conn.recv(65536))
                wire = bytes(frame)
                header = wire.index(b"PUB ")
                body_start = wire.index(b"\r\n", header) + 2
                size = int(wire[header:body_start].split()[-1])
                captured.append(json.loads(wire[body_start:body_start + size]))
                conn.sendall(b"PONG\r\n")
        finally:
            listener.close()

    thread = threading.Thread(target=broker, daemon=True)
    thread.start()
    env = {"HOOK_HUB_PUBLISH": "true", "BLOODBANK_ASM": "false",
           "BLOODBANK_NATS_HOST": "127.0.0.1", "BLOODBANK_NATS_PORT": str(port),
           "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")}
    with HubHarness(tmp_path, "", env) as hub:
        payload = {"session_id": "native-central-test", "tool_use_id": "one-call",
                   "tool_name": "Bash", "tool_input": {"command": "fixture"},
                   "tool_response": {"stdout": "ok", "exit_code": 0}}
        began = time.monotonic()
        first = request(hub, cli="codex", native="PostToolUse", payload=payload)
        assert time.monotonic() - began < .5
        second = request(hub, cli="codex", native="PostToolUse", payload=payload)
        assert second["deduplicated"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            receipt = detail(tmp_path, first["invocation_id"])
            if receipt["status"] != "received":
                break
            time.sleep(.02)
        assert receipt["status"] == "succeeded", receipt
        assert receipt["executions"][0]["handler_id"] == "bloodbank-publisher"
        assert receipt["executions"][0]["publish_status"] == "sent"
        assert len(captured) == 1
        assert captured[0]["actor"]["cli"] == "codex"
        assert captured[0]["type"] == "bloodbank.agent.tool.completed"
        assert receipt["executions"][0]["event_id"] == captured[0]["id"]
    thread.join(timeout=4)
