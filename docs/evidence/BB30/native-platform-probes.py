"""Independent BB-30 spec checks using unmodified pinned native platform method."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import sys
import types
from pathlib import Path
from typing import Any, Optional

import pytest
import conftest
from conftest import FixtureRunner, MessageEvent, MessageType, ProcessingOutcome, SendResult, valid_command
from test_adapter import FakeMessage, make_adapter
from test_final_answer import BODY, wire
from test_hermes_seam import native, release, compile_nodes
from test_native_outcome import native_finish
from bloodbank_hermes_gateway.contract import MESSAGE_APPENDED, validate_fact


@pytest.fixture
def actual_platform(release, monkeypatch):
    ns = {
        "asyncio": asyncio, "Any": Any, "Optional": Optional,
        "MessageEvent": MessageEvent, "MessageType": MessageType,
        "ProcessingOutcome": ProcessingOutcome, "inspect": inspect,
        "logger": logging.getLogger("bb30-independent-native"), "Path": Path,
        "EphemeralReply": type("EphemeralReply", (), {}),
        "_thread_metadata_for_source": lambda *args: {},
        "_reply_anchor_for_event": lambda event: event.message_id,
        "_strip_media_directives": lambda text: text,
        "_mark_notify_metadata": lambda metadata: metadata,
    }
    compile_nodes(release, "gateway/platforms/base.py",
                  {"_process_message_background", "_run_processing_hook"}, ns,
                  owner="BasePlatformAdapter")

    async def noop(*args, **kwargs):
        return None

    async def actual_ingress(adapter, event):
        key = adapter._event_session_key(event)
        event._gateway_accepted = True
        adapter._pending_messages = {}
        adapter._streaming_tts_completed_turns = set()
        adapter._expected_cancelled_tasks = set()
        adapter._session_tasks = {}
        adapter._active_sessions[key] = asyncio.Event()
        adapter.typed_command_prefix = "!"
        adapter.config.typing_indicator = False
        adapter._stop_typing_refresh = noop
        adapter._flush_text_debounce_now = noop
        adapter.on_processing_start = noop
        adapter._unwrap_ephemeral = lambda response: (response, 0)
        adapter.extract_media = lambda text: ([], text)
        adapter.filter_media_delivery_paths = lambda paths: paths
        adapter.extract_images = lambda text: ([], text)
        adapter.extract_local_files = lambda text: ([], text)
        adapter.filter_local_delivery_paths = lambda paths: paths
        adapter._final_delivery_adapter = lambda source: adapter
        adapter._send_with_retry = adapter.send
        adapter._get_human_delay = lambda: 0
        adapter._streaming_tts_turn_key = lambda *args, **kwargs: "offline"
        adapter._run_processing_hook = types.MethodType(ns["_run_processing_hook"], adapter)
        task = asyncio.create_task(ns["_process_message_background"](adapter, event, key))
        adapter._session_tasks[key] = task
        def cleanup(key, guard):
            if adapter._active_sessions.get(key) is guard:
                del adapter._active_sessions[key]
            adapter._session_tasks.pop(key, None)
        adapter._cleanup_finished_session_task = cleanup
        adapter._background_tasks.add(task)
        task.add_done_callback(adapter._background_tasks.discard)
        # Expected cancellation is how native stop/teardown classifies CANCELLED.
        if getattr(adapter, "review_expected_cancel", False):
            adapter._expected_cancelled_tasks.add(task)

    ledger = types.ModuleType("gateway.delivery_ledger")
    ledger.compute_obligation_id = lambda *args: "offline-unused"
    ledger.ledger_enabled = lambda: False
    ledger.mark_attempting = lambda *args, **kwargs: None
    ledger.record_obligation = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "gateway.delivery_ledger", ledger)
    monkeypatch.setattr(conftest.BasePlatformAdapter, "handle_message", actual_ingress)
    return ns


async def recover(tmp_path, command, expected):
    for _ in range(2):
        restarted = make_adapter(tmp_path)
        async def forbidden(event):
            raise AssertionError("native handler must never rerun after capture")
        restarted.set_message_handler(FixtureRunner(forbidden).handle_message)
        replay = FakeMessage(command)
        await asyncio.wait_for(restarted._handle_broker_message(replay), 5)
        assert replay.acked == 1 and replay.nacked == 0
        assert [wire(event) for event in restarted._js.events] == expected
    return restarted


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["success", "send_failure", "unexpected_cancel", "expected_cancel", "handler_failure"])
async def test_complete_native_platform_outcome_and_replay(actual_platform, native, tmp_path, valid_command, case):
    adapter = make_adapter(tmp_path)
    count = 0
    capture_bytes = []
    original_send = adapter.send
    adapter.review_expected_cancel = case == "expected_cancel"

    async def send(*args, **kwargs):
        if case == "send_failure":
            return SendResult(False, error="offline send failure")
        if case in {"unexpected_cancel", "expected_cancel"}:
            raise asyncio.CancelledError
        return await original_send(*args, **kwargs)

    adapter.send = send
    async def execute(event):
        nonlocal count
        count += 1
        result = native_finish(native)
        row = adapter.execution_state.get(valid_command["command_id"])
        assert row.capture_outcome == "success"
        assert row.state == "started" and row.outcome is None and not row.terminal_events
        assert row.captured_events[0]["data"]["text"] == result["final_response"] == BODY
        capture_bytes.extend(wire(event) for event in row.captured_events)
        if case == "handler_failure":
            raise RuntimeError("offline after finalizer")
        return result["final_response"]

    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(valid_command)
    await asyncio.wait_for(adapter._handle_broker_message(message), 5)
    row = adapter.execution_state.get(valid_command["command_id"])
    assert message.acked == 1 and message.nacked == 0
    assert row.outcome == ("success" if case == "success" else "cancelled" if case == "expected_cancel" else "failure")
    assert [wire(event) for event in row.captured_events] == capture_bytes
    assert sum(event["type"] == MESSAGE_APPENDED for event in row.terminal_events) == (case == "success")
    for event in (*row.started_events, *row.terminal_events):
        validate_fact(event)
    await recover(tmp_path, valid_command, [wire(event) for event in (*row.started_events, *row.terminal_events)])
    assert count == 1
    print(json.dumps({"probe": "complete-native-platform", "case": case, "outcome": row.outcome,
                      "answers": sum(e["type"] == MESSAGE_APPENDED for e in row.terminal_events),
                      "capture_preserved": True, "executions": count, "exact_replays": 2}))


@pytest.mark.asyncio
async def test_unknown_capture_recovery_write_failure_and_puback_failure(native, tmp_path, valid_command):
    first = make_adapter(tmp_path)
    ready, resume = asyncio.Event(), asyncio.Event()
    count = 0
    async def execute(event):
        nonlocal count
        count += 1
        result = native_finish(native)
        ready.set()
        await resume.wait()
        return result["final_response"]
    first.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(valid_command)
    running = asyncio.create_task(first._handle_broker_message(message))
    await asyncio.wait_for(ready.wait(), 5)
    initial = first.execution_state.get(valid_command["command_id"])
    captured = [wire(event) for event in initial.captured_events]
    assert initial.state == "started" and not initial.terminal_events

    restarted = make_adapter(tmp_path)
    def unavailable(**kwargs):
        raise OSError("offline recovery commit failure")
    restarted.execution_state.mark_completed = unavailable
    failed_write = FakeMessage(valid_command)
    await restarted._handle_broker_message(failed_write)
    assert failed_write.nacked == 1 and failed_write.acked == 0
    assert restarted.execution_state.get(valid_command["command_id"]) == initial

    restarted = make_adapter(tmp_path)
    original = restarted._js.publish
    async def publish(subject, payload, **kwargs):
        if json.loads(payload)["type"] == "bloodbank.agent.invocation.failed":
            assert restarted.execution_state.get(valid_command["command_id"]).failure_code == "processing_outcome_unknown"
            raise RuntimeError("offline recovery terminal PubAck failure")
        return await original(subject, payload, **kwargs)
    restarted._js.publish = publish
    failed_publish = FakeMessage(valid_command)
    await restarted._handle_broker_message(failed_publish)
    assert failed_publish.nacked == 1 and failed_publish.acked == 0
    row = restarted.execution_state.get(valid_command["command_id"])
    assert row.outcome == "failure" and row.failure_code == "processing_outcome_unknown"
    assert [wire(event) for event in row.captured_events] == captured
    assert not any(event["type"] == MESSAGE_APPENDED for event in row.terminal_events)
    await recover(tmp_path, valid_command, [wire(event) for event in (*row.started_events, *row.terminal_events)])
    resume.set()
    await asyncio.wait_for(running, 5)
    assert message.nacked == 1 and message.acked == 0 and count == 1
    print(json.dumps({"probe": "pending-recovery-double-fault", "capture_preserved": True,
                      "outcome": row.outcome, "failure_code": row.failure_code, "executions": count,
                      "answers": 0, "exact_replays": 2}))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["answer", "invocation", "turn", "ack"])
async def test_complete_native_platform_puback_and_ack_fault_replay(actual_platform, native, tmp_path, valid_command, stage):
    adapter = make_adapter(tmp_path)
    count = 0
    original_publish = adapter._js.publish
    fail_type = {
        "answer": MESSAGE_APPENDED,
        "invocation": "bloodbank.agent.invocation.completed",
        "turn": "bloodbank.conversation.turn.completed",
    }.get(stage)

    async def execute(event):
        nonlocal count
        count += 1
        return native_finish(native)["final_response"]

    async def publish(subject, payload, **kwargs):
        if json.loads(payload)["type"] == fail_type:
            row = adapter.execution_state.get(valid_command["command_id"])
            assert row.state == "completed" and row.outcome == "success"
            assert wire(row.terminal_events[0]) == wire(row.captured_events[0])
            raise RuntimeError("offline PubAck failure")
        return await original_publish(subject, payload, **kwargs)

    adapter._js.publish = publish
    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(valid_command, fail_ack=stage == "ack")
    await asyncio.wait_for(adapter._handle_broker_message(message), 5)
    row = adapter.execution_state.get(valid_command["command_id"])
    assert row.state == "completed" and row.outcome == "success"
    assert message.nacked == 1 and message.acked == 0
    await recover(tmp_path, valid_command, [wire(event) for event in (*row.started_events, *row.terminal_events)])
    assert count == 1
    print(json.dumps({"probe": "complete-native-platform-puback-ack", "stage": stage,
                      "outcome": row.outcome, "naks": message.nacked, "executions": count,
                      "answer_exact": row.terminal_events[0]["data"]["text"] == BODY, "exact_replays": 2}))


def test_canonical_loaded_bytes_identity():
    from bloodbank_hermes_gateway import adapter, contract
    from core import validate
    root = Path("/home/delorenj/code/33GOD/bloodbank/.worktrees/feat-bb-30-final-assistant-publication")
    assert Path(adapter.__file__).resolve() == root / "services/hermes-gateway/bloodbank_hermes_gateway/adapter.py"
    assert Path(contract.__file__).resolve() == root / "services/hermes-gateway/bloodbank_hermes_gateway/contract.py"
    assert Path(validate.__file__).resolve() == root / "services/agent-hooks/core/validate.py"
    assert validate._schemas_root().resolve() == root / "schemas"
    print(json.dumps({"probe": "source-identity", "adapter": adapter.__file__, "validator": validate.__file__, "schemas": str(validate._schemas_root())}))
