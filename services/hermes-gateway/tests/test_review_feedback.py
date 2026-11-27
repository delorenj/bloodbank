"""BB-67 disposable frozen-row and gateway fault probes (no live bus/profile)."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from bloodbank_hermes_gateway.adapter import BloodbankAdapter
from bloodbank_hermes_gateway.contract import FEEDBACK, CommandInvalid, Invocation, decode_command, started_events
from bloodbank_hermes_gateway.execution_state import envelope_digest
from test_adapter import FakeMessage, FakeJetStream
from test_final_answer import BODY, capture

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent-hooks"))
from core.review_feedback import build_command, evidence_state, feedback_digest
from core.validate import ContractViolation, EnvelopeInvalid, validate_envelope


def frozen():
    row = {
        "submissionId": str(uuid.uuid4()),
        "projectId": "greeninbetween", "revisionId": "b" * 64,
        "artifactId": "artifact-1", "author": "reviewer@example.com",
        "recipient": "green-in-between-pm", "note": "Please inspect the next scene.",
        "annotations": [{"id": "saved-1", "version": 2,
                         "author": "reviewer@example.com", "kind": "text",
                         "anchor": {"quote": "scene"}, "comment": "Spacing is off",
                         "updatedAt": "2026-11-27T12:00:00Z"}],
        "correlationId": str(uuid.uuid4()), "createdAt": "2026-11-27T12:00:00Z",
    }
    row["digest"] = feedback_digest(row)
    return row


def adapter(tmp_path, monkeypatch, *, target="green-in-between-pm"):
    import bloodbank_hermes_gateway.adapter as adapter_module
    monkeypatch.setattr(adapter_module, "profile_exists", lambda p: p == "green-in-between-pm")
    config = SimpleNamespace(enabled=True, extra={
        "target_profiles": {"green-in-between-pm": target},
        "fleet_registry": str(tmp_path / "absent.yaml"),
        "execution_state_file": str(tmp_path / "feedback-journal.sqlite3"),
        "feedback": {"enabled": True, "ack_wait_seconds": 10, "ack_progress_seconds": 1},
    })
    instance = BloodbankAdapter(config)
    instance._js = FakeJetStream()
    instance.ack_progress_seconds = .01
    return instance


def test_frozen_command_is_stable_and_private():
    row = frozen()
    first = build_command(row)
    assert first == build_command({**row, "state": "published", "updatedAt": "later"})
    assert first["data"] == {key: row[key] for key in (
        "submissionId", "projectId", "revisionId", "artifactId", "author",
        "recipient", "note", "annotations", "digest", "correlationId"
    )}
    assert first["causationid"] == row["submissionId"]
    assert first["command_id"] == Invocation.from_envelope(first, "green-in-between-pm", "feedback").turn_id
    assert FEEDBACK.subject == first["subject"]
    assert decode_command(json.dumps(first).encode(), lane=FEEDBACK) == first
    assert "prompt_text" not in started_events(Invocation.from_envelope(first, "green-in-between-pm", "feedback"))[0]["data"]


@pytest.mark.parametrize("change", ["digest", "recipient", "projectId", "annotations", "causationid", "id", "command_id", "actor", "type"])
def test_conflicting_or_misdirected_row_is_rejected(change):
    row = frozen()
    command = build_command(row)
    altered = copy.deepcopy(command)
    if change == "annotations":
        altered["data"]["annotations"][0]["version"] = 3
    elif change in {"digest", "recipient", "projectId"}:
        altered["data"][change] = "invalid"
    elif change == "actor":
        altered["actor"]["agent_id"] = "different@example.com"
    elif change == "type":
        altered["type"] = "bloodbank.agent.invocation.invoke"
    else:
        altered[change] = str(uuid.uuid4())
    with pytest.raises((ContractViolation, EnvelopeInvalid, CommandInvalid, ValueError)):
        decode_command(json.dumps(altered).encode(), lane=FEEDBACK)


@pytest.mark.asyncio
async def test_actual_final_answer_only_joins_matching_feedback(tmp_path, monkeypatch):
    row = frozen()
    command = build_command(row)
    gateway = adapter(tmp_path, monkeypatch)
    calls = 0
    async def handler(event):
        nonlocal calls
        calls += 1
        assert event.source.profile == "green-in-between-pm"
        assert event.raw_message["command_id"] == command["command_id"]
        assert row["submissionId"] in event.text
        capture()
        return "local send is not the native answer"
    gateway.set_message_handler(handler)
    msg = FakeMessage(command)
    await gateway._handle_broker_message(msg, gateway.lanes[-1])
    assert msg.acked == 1 and calls == 1
    events = gateway._js.events
    assert [evidence_state(command, e, profile="green-in-between-pm") for e in events] == [
        "queued", None, "running", "answered", None, None
    ]
    answer = next(e for e in events if e["type"] == "bloodbank.conversation.message.appended")
    assert answer["data"]["text"] == BODY
    assert answer["data"]["native_session_id"] == "hermes-native-session"
    assert answer["data"]["native_turn_id"] == "hermes-native-turn"
    assert answer["data"]["context"]["review_feedback"]["submissionId"] == row["submissionId"]
    assert evidence_state(command, answer, profile="bloodbank-pm") is None
    wrong = build_command(frozen())
    assert evidence_state(wrong, answer, profile="green-in-between-pm") is None
    for mutation in ("text", "native_turn_id", "command_event_id", "issuer", "context"):
        unrelated = copy.deepcopy(answer)
        unrelated["data"][mutation] = (
            "[SILENT]" if mutation == "text" else "" if mutation == "native_turn_id"
            else str(uuid.uuid4()) if mutation == "command_event_id"
            else {"type": "operator", "agent_id": "different@example.com"} if mutation == "issuer"
            else {"review_feedback": {"submissionId": "wrong"}}
        )
        assert evidence_state(command, unrelated, profile="green-in-between-pm") is None
    replay = FakeMessage(command)
    await gateway._handle_broker_message(replay, gateway.lanes[-1])
    assert replay.acked == 1 and calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["queued", "answer", "ack"])
async def test_publish_and_ack_faults_replay_without_second_turn(tmp_path, monkeypatch, stage):
    command = build_command(frozen())
    first = adapter(tmp_path, monkeypatch)
    calls = 0
    async def handler(_event):
        nonlocal calls
        calls += 1
        capture()
        return "local"
    first.set_message_handler(handler)
    initial = FakeMessage(command, fail_ack=stage == "ack")
    publish = first._js.publish
    async def break_publish(subject, payload, **kwargs):
        if json.loads(payload)["type"] == {
            "queued": "bloodbank.agent.invocation.queued",
            "answer": "bloodbank.conversation.message.appended",
        }.get(stage):
            raise RuntimeError("lost PubAck")
        await publish(subject, payload, **kwargs)
    first._js.publish = break_publish
    await first._handle_broker_message(initial, first.lanes[-1])
    assert initial.nacked == 1
    assert calls == (0 if stage == "queued" else 1)
    second = adapter(tmp_path, monkeypatch)
    second.set_message_handler(handler)
    replay = FakeMessage(command)
    await second._handle_broker_message(replay, second.lanes[-1])
    assert replay.acked == 1 and calls == 1
    resolved = second.execution_state.get(command["command_id"])
    assert resolved is not None and resolved.state == "completed"
    if stage == "queued":
        assert any(e["type"] == "bloodbank.conversation.message.appended" for e in second._js.events)
    else:
        original = first.execution_state.get(command["command_id"])
        assert original is not None
        assert tuple(second._js.events[-3:]) == original.terminal_events


@pytest.mark.asyncio
async def test_started_without_capture_is_uncertain_and_never_reexecuted(tmp_path, monkeypatch):
    command = build_command(frozen())
    first = adapter(tmp_path, monkeypatch)
    invocation = Invocation.from_envelope(command, "green-in-between-pm", "feedback")
    digest = envelope_digest(command)
    first.execution_state.claim_pending(
        command_id=command["command_id"], digest=digest, profile=invocation.profile,
        queued_events=(), lane="feedback",
    )
    first.execution_state.mark_started(
        command_id=command["command_id"], digest=digest,
        started_events=started_events(invocation),
    )
    second = adapter(tmp_path, monkeypatch)
    calls = 0
    async def handler(_event):
        nonlocal calls
        calls += 1
        return "must not execute"
    second.set_message_handler(handler)
    msg = FakeMessage(command)
    await second._handle_broker_message(msg, second.lanes[-1])
    assert msg.acked == 1 and calls == 0
    stored = second.execution_state.get(command["command_id"])
    assert stored.state == "completed" and stored.failure_code == "feedback_execution_uncertain"
    failed = next(e for e in second._js.events if e["type"] == "bloodbank.agent.invocation.failed")
    assert evidence_state(command, failed, profile="green-in-between-pm") == "uncertain"


@pytest.mark.asyncio
async def test_duplicate_claim_and_profile_conflict(tmp_path, monkeypatch):
    command = build_command(frozen())
    gateway = adapter(tmp_path, monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    async def handler(_event):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        capture()
        return "local"
    gateway.set_message_handler(handler)
    first = FakeMessage(command)
    task = asyncio.create_task(gateway._handle_broker_message(first, gateway.lanes[-1]))
    await asyncio.wait_for(started.wait(), timeout=5)
    duplicate = FakeMessage(command)
    await gateway._handle_broker_message(duplicate, gateway.lanes[-1])
    assert duplicate.nacked == 1 and calls == 1
    conflict = copy.deepcopy(command)
    conflict["data"]["annotations"][0]["comment"] = "changed under same id/version"
    changed = FakeMessage(conflict)
    await gateway._handle_broker_message(changed, gateway.lanes[-1])
    assert changed.termed == 1 and calls == 1
    release.set()
    await asyncio.wait_for(task, timeout=5)
    assert first.acked == 1

    wrong_dir = tmp_path / "wrong-route"
    wrong_dir.mkdir()
    bad = adapter(wrong_dir, monkeypatch, target="bloodbank-pm")
    monkeypatch.setattr(bad.profile_resolver, "profile_exists", lambda _p: True)
    bad.set_message_handler(handler)
    refused = FakeMessage(command)
    await bad._handle_broker_message(refused, bad.lanes[-1])
    assert refused.termed == 1 and calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["disabled", "missing"])
async def test_disabled_recipient_and_cancelled_turn_keep_submission(tmp_path, monkeypatch, route):
    command = build_command(frozen())
    gateway = adapter(tmp_path, monkeypatch)
    gateway.profile_resolver.target_profiles.clear()
    (tmp_path / "absent.yaml").write_text(json.dumps({
        "schema_version": 1,
        "agents": {"green-in-between-pm": {
            "profile_name": "green-in-between-pm", "bloodbank": {
                "enabled": False, "gateway_scope": "fleet",
                "target_agent_id": "green-in-between-pm",
            }
        }}
    }))
    if route == "missing":
        (tmp_path / "absent.yaml").write_text(json.dumps({"schema_version": 1, "agents": {}}))
    called = 0
    async def handler(_event):
        nonlocal called
        called += 1
        raise asyncio.CancelledError
    gateway.set_message_handler(handler)
    refused = FakeMessage(command)
    await gateway._handle_broker_message(refused, gateway.lanes[-1])
    assert refused.termed == 1 and called == 0
    stored = gateway.execution_state.get(command["command_id"])
    assert stored is not None and stored.state == "rejected_pre_start"
    failed = next(e for e in gateway._js.events if e["type"] == "bloodbank.agent.invocation.failed")
    assert evidence_state(command, failed, profile="green-in-between-pm") == "failed"
    assert command["data"]["note"] == "Please inspect the next scene."

    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    allowed = adapter(allowed_dir, monkeypatch)
    allowed.set_message_handler(handler)
    cancelled = FakeMessage(command)
    await allowed._handle_broker_message(cancelled, allowed.lanes[-1])
    assert cancelled.acked == 1 and called == 1
    assert not any(e["type"] == "bloodbank.conversation.message.appended" for e in allowed._js.events)
    assert allowed.execution_state.get(command["command_id"]).outcome == "cancelled"
    failed = next(e for e in allowed._js.events if e["type"] == "bloodbank.agent.invocation.failed")
    assert evidence_state(command, failed, profile="green-in-between-pm") == "failed"


def test_cli_check_and_unconfirmed_publication(tmp_path):
    row = frozen()
    root = Path(__file__).resolve().parents[3]
    cli = root / "bin/bb-emit"
    cmd = [sys.executable, str(cli), "--type", "bloodbank.review.feedback.send",
           "--feedback-submission"]
    env = {**os.environ, "BLOODBANK_SCHEMAS_DIR": str(root / "schemas")}
    body = json.dumps(row)
    first = subprocess.run(cmd + ["--check"], input=body, text=True,
                           capture_output=True, env=env, timeout=15)
    second = subprocess.run(cmd + ["--check"], input=body, text=True,
                            capture_output=True, env=env, timeout=15)
    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    assert json.loads(first.stdout)["command_id"] == build_command(row)["command_id"]
    # An unreachable loopback endpoint must fail closed even without --strict.
    unreachable = subprocess.run(cmd + ["--host", "127.0.0.1", "--port", "1"],
                                 input=body, text=True, capture_output=True,
                                 env=env, timeout=15)
    assert unreachable.returncode != 0
    assert "UNCONFIRMED" in unreachable.stderr
    assert "published" not in unreachable.stdout
    bare = subprocess.run([sys.executable, str(cli), "--type", "bloodbank.review.feedback.send",
                           "--kind", "command", "--check"], input=body, text=True,
                          capture_output=True, env=env, timeout=15)
    assert bare.returncode != 0
    invalid = copy.deepcopy(row)
    invalid["note"] = "PRIVATE_REVIEW_MARKER" * 300
    invalid["digest"] = feedback_digest(invalid)
    rejected = subprocess.run(cmd + ["--check"], input=json.dumps(invalid), text=True,
                              capture_output=True, env=env, timeout=15)
    assert rejected.returncode != 0 and "PRIVATE_REVIEW_MARKER" not in rejected.stderr
    assert not rejected.stdout


@pytest.mark.parametrize("puback", [
    {"stream": "BLOODBANK_COMMANDS", "seq": 7},
    {"stream": "WRONG", "seq": 7},
    {"stream": "BLOODBANK_COMMANDS", "seq": 0},
    {"stream": "BLOODBANK_COMMANDS", "seq": True},
    {"error": {"code": 503}},
    None,
])
def test_puback_frame_required_not_core_pong(tmp_path, puback):
    root = Path(__file__).resolve().parents[3]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        observed = []
        def fake_broker():
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(5)
                conn.sendall(b"INFO {}\r\n")
                wire = b""
                while b"PING\r\n" not in wire:
                    chunk = conn.recv(4096)
                    if not chunk:
                        raise AssertionError("client closed before readiness")
                    wire += chunk
                subscription = next(line.split()[1] for line in wire.split(b"\r\n") if line.startswith(b"SUB "))
                conn.sendall(b"PONG\r\n")
                while b"PUB " not in wire or b"\r\n{" not in wire:
                    chunk = conn.recv(4096)
                    if not chunk:
                        raise AssertionError("client closed before publication")
                    wire += chunk
                observed.append(wire)
                if puback is not None:
                    payload = json.dumps(puback).encode()
                    # Require handling server keepalives and split TCP bodies.
                    conn.sendall(b"PING\r\nMSG " + subscription + b" 1 " + str(len(payload)).encode() + b"\r\n" + payload[:4])
                    conn.sendall(payload[4:] + b"\r\n")
                    assert conn.recv(4096) == b"PONG\r\n"
        worker = threading.Thread(target=fake_broker, daemon=True)
        worker.start()
        row = frozen()
        cmd = [sys.executable, str(root / "bin/bb-emit"), "--type", "bloodbank.review.feedback.send",
               "--feedback-submission", "--host", "127.0.0.1", "--port", str(port)]
        result = subprocess.run(cmd, input=json.dumps(row), text=True, capture_output=True,
                                env={**os.environ, "BLOODBANK_SCHEMAS_DIR": str(root / "schemas")}, timeout=10)
        worker.join(timeout=5)
    if puback == {"stream": "BLOODBANK_COMMANDS", "seq": 7}:
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["seq"] == 7 and receipt["command_id"] == build_command(row)["command_id"]
    else:
        assert result.returncode != 0 and "UNCONFIRMED" in result.stderr
        assert not result.stdout
    assert observed and b"PUB bloodbank.cmd.review.feedback.send" in observed[0]


def test_multi_author_row_preserved_and_node_digest_agrees():
    row = frozen()
    row["note"] = "Unicode café 🐦\nline"
    row["annotations"][0]["author"] = "other-reviewer@example.com"
    row["digest"] = feedback_digest(row)
    # The owner computes this digest with JSON.stringify, not Python JSON.
    result = subprocess.run(
        ["node", "-e", "const c=require('node:crypto');let s='';process.stdin.on('data',x=>s+=x);process.stdin.on('end',()=>{const r=JSON.parse(s);process.stdout.write(c.createHash('sha256').update(JSON.stringify({note:r.note,annotations:r.annotations.map(a=>[a.id,a.version])})).digest('hex'))})"],
        input=json.dumps(row), text=True, capture_output=True, check=True, timeout=10,
    )
    assert result.stdout == row["digest"]
    assert build_command(row)["data"]["annotations"][0]["author"] == "other-reviewer@example.com"


@pytest.mark.parametrize("change", ["revision", "long_note", "blank_comment", "bad_kind", "bad_anchor", "duplicate", "time", "oversize"])
def test_invalid_saved_shapes_fail_before_publish(change):
    row = frozen()
    if change == "revision":
        row["revisionId"] = "revision-1"
    elif change == "long_note":
        row["note"] = "x" * 4001
    elif change == "blank_comment":
        row["annotations"][0]["comment"] = " \n"
    elif change == "bad_kind":
        row["annotations"][0]["kind"] = "invented"
    elif change == "bad_anchor":
        row["annotations"][0]["anchor"] = None
    elif change == "duplicate":
        row["annotations"] *= 2
    elif change == "time":
        row["createdAt"] = "not-a-time"
    else:
        row["annotations"][0]["anchor"] = {"quote": "x" * 262144}
    row["digest"] = feedback_digest(row)
    with pytest.raises((ValueError, EnvelopeInvalid, ContractViolation)):
        build_command(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", " \n", "[SILENT]", "NO_REPLY"])
async def test_feedback_empty_silent_capture_cannot_answer(tmp_path, monkeypatch, body):
    command = build_command(frozen())
    gateway = adapter(tmp_path, monkeypatch)
    async def handler(_event):
        capture(body)
        return "not a native answer"
    gateway.set_message_handler(handler)
    msg = FakeMessage(command)
    await gateway._handle_broker_message(msg, gateway.lanes[-1])
    assert msg.acked == 1
    assert not any(e["type"] == "bloodbank.conversation.message.appended" for e in gateway._js.events)
    assert "answered" not in [evidence_state(command, e, profile="green-in-between-pm") for e in gateway._js.events]
    failed = next(e for e in gateway._js.events if e["type"] == "bloodbank.agent.invocation.failed")
    assert failed["data"]["error_code"] == "feedback_final_answer_missing"
    assert evidence_state(command, failed, profile="green-in-between-pm") == "failed"


@pytest.mark.asyncio
async def test_feedback_capture_before_processing_crash_is_uncertain(tmp_path, monkeypatch):
    command = build_command(frozen())
    first = adapter(tmp_path, monkeypatch)
    captured, pause = asyncio.Event(), asyncio.Event()
    async def handler(_event):
        capture()
        captured.set()
        await pause.wait()
        return "local"
    first.set_message_handler(handler)
    msg = FakeMessage(command)
    task = asyncio.create_task(first._handle_broker_message(msg, first.lanes[-1]))
    await asyncio.wait_for(captured.wait(), 5)
    stored = first.execution_state.get(command["command_id"])
    assert stored is not None and stored.capture_outcome == "success" and stored.state == "started"
    second = adapter(tmp_path, monkeypatch)
    async def forbidden(_event):
        raise AssertionError("must not execute after captured turn")
    second.set_message_handler(forbidden)
    try:
        replay = FakeMessage(command)
        await second._handle_broker_message(replay, second.lanes[-1])
        assert replay.acked == 1
        failed = next(e for e in second._js.events if e["type"] == "bloodbank.agent.invocation.failed")
        assert evidence_state(command, failed, profile="green-in-between-pm") == "uncertain"
        assert not any(e["type"] == "bloodbank.conversation.message.appended" for e in second._js.events)
    finally:
        pause.set()
        await asyncio.wait_for(task, 5)


def test_opt_in_lane_and_wrong_profile_refused(tmp_path, monkeypatch):
    instance = adapter(tmp_path, monkeypatch)
    assert [l.name for l in instance.lanes] == ["batch", "feedback"]
    with pytest.raises(ValueError, match="shutdown_interrupt"):
        BloodbankAdapter(SimpleNamespace(extra={
            "execution_state_file": str(tmp_path / "other.sqlite3"),
            "feedback": {"enabled": True, "shutdown_interrupt": "retry"},
        }))
