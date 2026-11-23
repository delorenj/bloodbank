"""Offline regressions using the complete pinned Hermes native finalizer.

The platform fixture models the pinned base adapter's post-finalizer send and
processing-complete outcomes; no model, broker or running gateway is contacted.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

# Pytest injects the imported pinned-native fixtures by these names.
# ruff: noqa: F811
import pytest
from bloodbank_hermes_gateway import plugin
from bloodbank_hermes_gateway.adapter import BloodbankAdapter
from bloodbank_hermes_gateway.contract import MESSAGE_APPENDED
from conftest import FixtureRunner, SendResult
from test_adapter import (
    FakeMessage,
    _session_of,
    lane,
    make_adapter,
    make_timeout_adapter,
    memo_command,
    stopping_runner,
)
from test_final_answer import BODY, wire
from test_hermes_seam import (
    agent_fixture,
    compile_nodes,
    home_scope,
    native,  # noqa: F401 - pytest fixture
    release,  # noqa: F401 - pytest fixture
)


def native_finish(native, *, interrupted=False, failed=False, text=BODY):
    agent = agent_fixture()
    agent._interrupt_message = None
    params = {
        "agent": agent, "effective_task_id": "outcome-task", "turn_id": "outcome-turn",
        "original_user_message": "offline proof", "messages": [], "conversation_history": [],
    }
    eval(native.pre, dict(params, _invoke_hook=native.ns["invoke_hook"]))
    return native.finalize(
        **params, final_response=text, api_call_count=1,
        interrupted=interrupted, failed=failed, user_message="offline proof",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )


def require_row(adapter, command):
    row = adapter.execution_state.get(command["command_id"])
    assert row is not None
    return row


def no_execution(_event):
    raise AssertionError("reran inference after durable native capture")


async def replay_exact(tmp_path, command, expected):
    for _ in range(2):
        restarted = make_adapter(tmp_path)
        restarted.set_message_handler(FixtureRunner(no_execution).handle_message)
        redelivery = FakeMessage(command)
        await restarted._handle_broker_message(redelivery)
        assert redelivery.acked == 1 and redelivery.nacked == 0
        row = require_row(restarted, command)
        assert [wire(e) for e in restarted._js.events] == [
            wire(e) for e in row.queued_events
        ] + expected


@pytest.mark.asyncio
async def test_actual_primary_handler_closure_accepts_and_captures(native, release, tmp_path, valid_command):
    from gateway import run

    ns = {
        "Path": Path, "get_hermes_home": native.constants.get_hermes_home,
        "_profile_runtime_scope": run._profile_runtime_scope, "SessionSource": Any,
        "Optional": Optional, "List": list, "Dict": dict, "Any": Any,
    }
    compile_nodes(
        release, "gateway/run.py",
        {"_primary_message_handler", "_make_default_profile_message_handler", "_run_agent"},
        ns, owner="GatewayRunner",
    )

    class Runner(FixtureRunner):
        _primary_message_handler = ns["_primary_message_handler"]
        _make_default_profile_message_handler = ns["_make_default_profile_message_handler"]
        _run_agent = ns["_run_agent"]

        async def _handle_message(self, event):
            return (await self._run_agent(event.text, "", [], event.source, "proof-session"))["final_response"]

        async def _run_agent_inner(self, *args, **kwargs):
            assert native.home.get() == str(tmp_path / "research")
            return native_finish(native)

    with home_scope(native.home, tmp_path / "research"):
        native.register()
    handler = Runner(None)._primary_message_handler()
    handler.native_handler = True  # Keep the actual closure, not the autouse wrapper.
    adapter = make_adapter(tmp_path, target_profiles={"bloodbank-pm": "research"})
    BloodbankAdapter.set_message_handler(adapter, handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    answer = next(e for e in adapter._js.events if e["type"] == MESSAGE_APPENDED)
    assert message.acked == 1 and answer["data"]["text"] == BODY
    assert plugin.command_binding.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("display", ["raw", "sanitized", "failed_turn"])
@pytest.mark.parametrize("reply", [
    "HTTP 429: no eligible route; " + "provider details\n" * 40,
    "Your request was not processed. Send it again if you still want me to carry it out.",
])
async def test_owning_native_early_return_without_finalizer_fails_closed(
    native, release, tmp_path, valid_command, reply, display
):
    """The pinned retry-exhaustion result precedes all finalizer callbacks."""
    import ast
    from gateway import run
    from test_hermes_seam import source

    tree = ast.parse(source(release, "agent/conversation_loop.py"))
    early = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.Return) and node.lineno == 6173)
    assert early.value is not None
    failed_expr = compile(ast.Expression(early.value), "pinned-native-early-return", "eval")
    ns = {
        "Path": Path, "get_hermes_home": native.constants.get_hermes_home,
        "_profile_runtime_scope": run._profile_runtime_scope, "SessionSource": Any,
        "Optional": Optional, "List": list, "Dict": dict, "Any": Any,
    }
    compile_nodes(release, "gateway/run.py",
                  {"_primary_message_handler", "_make_default_profile_message_handler", "_run_agent"},
                  ns, owner="GatewayRunner")
    results = []

    class Runner(FixtureRunner):
        _primary_message_handler = ns["_primary_message_handler"]
        _make_default_profile_message_handler = ns["_make_default_profile_message_handler"]
        _run_agent = ns["_run_agent"]

        async def _handle_message(self, event):
            result = await self._run_agent(event.text, "", [], event.source, "proof-session")
            results.append(result)
            if display == "raw":
                return result["final_response"]
            if display == "sanitized":
                return "⏱️ The AI model service is rate-limiting requests. Wait a moment, then use /retry."
            return (
                "A sanitized provider warning.\n\nYour request was not processed. "
                "Send it again if you still want me to carry it out."
            )

        async def _run_agent_inner(self, *args, **kwargs):
            return eval(failed_expr, dict(
                _final_response=reply, messages=[], api_call_count=1,
                _final_summary=reply, classified=type("Reason", (), {
                    "reason": type("Value", (), {"value": "rate_limit"})()
                })(), _billing_unverified=False, _billing_block=None,
            ))

    adapter = make_adapter(tmp_path, target_profiles={"bloodbank-pm": "research"})
    with home_scope(native.home, tmp_path / "research"):
        native.register()
    handler = Runner(None)._primary_message_handler()
    handler.native_handler = True
    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    row = require_row(adapter, valid_command)
    assert results[0]["failed"] is True and results[0]["completed"] is False
    assert message.acked == 1 and message.nacked == 0
    assert row.capture_outcome is None and row.outcome == "failure"
    assert row.failure_code == "hermes_turn_error"
    assert not any(e["type"] == MESSAGE_APPENDED for e in adapter._js.events)
    assert row.terminal_events[0]["type"] == "bloodbank.agent.invocation.failed"
    await replay_exact(tmp_path, valid_command,
                       [wire(e) for e in (*row.started_events, *row.terminal_events)])


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("answer_text", [
    "HTTP429:no eligible route; this is the error we saw yesterday. The new route now works.",
    "HTTP 429: no eligible route; this is the error we saw yesterday. The new route now works.",
    "HTTP 400: provider rejected request was the old log entry. I fixed the configuration; the bounded tool now works.",
    "API call failed: HTTP 429 was yesterday. I retried the tool and it now succeeds.",
    "API call failed after 3 retries: HTTP 429 no eligible fast route\n" + "details\n" * 70,
    "HTTP 404 means the resource does not exist.",
    "⏱️ The AI model service is rate-limiting requests. Wait a moment, then use /retry.",
    "Your request was not processed. Send it again if you still want me to carry it out.",
    "Operation interrupted: handling API error (12.3s elapsed).",
])
async def test_owning_native_finalizer_flags_control_outcome_not_reply_text(
    native, release, tmp_path, valid_command, answer_text, failed
):
    from gateway import run
    ns = {
        "Path": Path, "get_hermes_home": native.constants.get_hermes_home,
        "_profile_runtime_scope": run._profile_runtime_scope, "SessionSource": Any,
        "Optional": Optional, "List": list, "Dict": dict, "Any": Any,
    }
    compile_nodes(release, "gateway/run.py",
                  {"_primary_message_handler", "_make_default_profile_message_handler", "_run_agent"},
                  ns, owner="GatewayRunner")

    class Runner(FixtureRunner):
        _primary_message_handler = ns["_primary_message_handler"]
        _make_default_profile_message_handler = ns["_make_default_profile_message_handler"]
        _run_agent = ns["_run_agent"]

        async def _handle_message(self, event):
            result = await self._run_agent(event.text, "", [], event.source, "proof-session")
            assert result["failed"] is failed
            return result["final_response"]

        async def _run_agent_inner(self, *args, **kwargs):
            return native_finish(native, text=answer_text, failed=failed)

    with home_scope(native.home, tmp_path / "research"):
        native.register()
    handler = Runner(None)._primary_message_handler()
    handler.native_handler = True
    adapter = make_adapter(tmp_path, target_profiles={"bloodbank-pm": "research"})
    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    row = require_row(adapter, valid_command)
    expected_outcome = "failure" if failed else "success"
    assert message.acked == 1 and message.nacked == 0
    assert row.outcome == row.capture_outcome == expected_outcome
    answers = [e for e in row.terminal_events if e["type"] == MESSAGE_APPENDED]
    if failed:
        assert answers == [] and row.captured_events == ()
        assert row.terminal_events[0]["type"] == "bloodbank.agent.invocation.failed"
    else:
        assert [e["data"]["text"] for e in answers] == [answer_text]
        assert row.terminal_events[1]["type"] == "bloodbank.agent.invocation.completed"
    await replay_exact(tmp_path, valid_command,
                       [wire(e) for e in (*row.started_events, *row.terminal_events)])


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["send_failure", "send_cancel", "handler_failure", "handler_cancel"])
@pytest.mark.parametrize("publication_failure", [False, True])
async def test_native_capture_then_actual_failed_processing_never_replays_answer(
    native, tmp_path, valid_command, stage, publication_failure
):
    adapter = make_adapter(tmp_path)
    original_publish = adapter._js.publish
    executions = 0
    captured_bytes = []

    async def failed_send(*args, **kwargs):
        if stage == "send_cancel":
            raise asyncio.CancelledError
        return SendResult(False, error="delivery failed")

    if stage.startswith("send_"):
        adapter.send = failed_send

    async def execute(_event):
        nonlocal executions
        executions += 1
        response = native_finish(native)["final_response"]
        row = require_row(adapter, valid_command)
        assert row.state == "started" and row.outcome is None
        assert row.capture_outcome == "success" and row.terminal_events == ()
        assert row.captured_events[0]["data"]["text"] == response == BODY
        assert len(adapter._js.events) == 3  # Queued and started, no answer or terminal yet.
        captured_bytes.extend(wire(e) for e in row.captured_events)
        if stage == "handler_failure":
            raise RuntimeError("platform handler failed after model finalized")
        if stage == "handler_cancel":
            raise asyncio.CancelledError
        return response

    async def publish(subject, payload, **kwargs):
        if publication_failure and json.loads(payload)["type"] == "bloodbank.agent.invocation.failed":
            # Known failure/cancellation must already be durable before PubAck.
            assert require_row(adapter, valid_command).state == "completed"
            raise RuntimeError("failed terminal PubAck")
        await original_publish(subject, payload, **kwargs)

    adapter._js.publish = publish
    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    assert message.acked == (0 if publication_failure else 1)
    assert message.nacked == (1 if publication_failure else 0)
    row = require_row(adapter, valid_command)
    assert row.state == "completed"
    assert row.outcome == ("cancelled" if stage.endswith("cancel") else "failure")
    assert not any(e["type"] == MESSAGE_APPENDED for e in (*row.terminal_events, *adapter._js.events))
    assert [wire(e) for e in row.captured_events] == captured_bytes
    expected = [wire(e) for e in (*row.started_events, *row.terminal_events)]
    await replay_exact(tmp_path, valid_command, expected)
    assert executions == 1


@pytest.mark.asyncio
async def test_native_finalizer_preserves_model_and_detached_terminal_metadata(native, tmp_path, valid_command):
    command = copy.deepcopy(valid_command)
    command["data"]["model"] = "automaticai/auto/fast"
    adapter = make_adapter(tmp_path)

    async def execute(event):
        assert event.turn_model == command["data"]["model"]
        record = adapter._records[event.message_id]
        assert record.dispatched_at is not None
        record.detached_at = record.dispatched_at + 0.001
        return native_finish(native)["final_response"]

    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(command)
    await adapter._handle_broker_message(message)
    row = require_row(adapter, command)
    assert message.acked == 1
    assert wire(row.terminal_events[0]) == wire(row.captured_events[0])
    terminal = row.terminal_events[1]
    data = terminal["data"]
    assert data["model_override"] == {"requested": command["data"]["model"], "applied": True}
    assert data["detached"] is True
    assert data["detached_after_seconds"] >= 0
    assert data["elapsed_seconds"] >= 0
    await replay_exact(tmp_path, command, [wire(e) for e in (*row.started_events, *row.terminal_events)])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fail", "retry"])
async def test_native_interrupted_finalizer_retains_shutdown_code_without_rerun(native, tmp_path, valid_command, mode):
    adapter = make_adapter(tmp_path)
    adapter._batch.shutdown_interrupt = mode
    runner = stopping_runner(adapter)
    entered, resume = asyncio.Event(), asyncio.Event()

    async def execute(event):
        entered.set()
        await resume.wait()
        return native_finish(native, interrupted=True)["final_response"]

    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(valid_command)
    task = asyncio.create_task(adapter._handle_broker_message(message))
    await asyncio.wait_for(entered.wait(), 2)
    runner.begin_stop()
    runner._running_agents[_session_of(adapter, valid_command)] = object()
    runner._interrupt_running_agents("Gateway restarting")
    resume.set()
    await asyncio.wait_for(task, 2)
    row = require_row(adapter, valid_command)
    assert message.acked == 1 and row.outcome == "failure"
    assert row.capture_outcome == "cancelled" and row.captured_events == ()
    assert row.failure_code == "interrupted_by_shutdown"
    assert row.terminal_events[0]["data"]["error_code"] == "interrupted_by_shutdown"
    assert not any(e["type"] == MESSAGE_APPENDED for e in adapter._js.events)
    await replay_exact(tmp_path, valid_command, [wire(e) for e in (*row.started_events, *row.terminal_events)])


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_processing", "resolution_write"])
async def test_native_captured_pending_restart_fails_closed_without_inference(native, tmp_path, valid_command, boundary):
    adapter = make_adapter(tmp_path)
    ready, resume = asyncio.Event(), asyncio.Event()

    async def execute(_event):
        response = native_finish(native)["final_response"]
        ready.set()
        if boundary == "before_processing":
            await resume.wait()
        return response

    if boundary == "resolution_write":
        def fail_write(**kwargs):
            raise OSError("outbox resolution unavailable")
        adapter.execution_state.mark_completed = fail_write
    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(valid_command)
    task = asyncio.create_task(adapter._handle_broker_message(message))
    await asyncio.wait_for(ready.wait(), 2)
    if boundary == "resolution_write":
        await asyncio.wait_for(task, 2)
        assert message.nacked == 1 and message.acked == 0
    captured = require_row(adapter, valid_command)
    assert captured.state == "started" and captured.outcome is None
    assert captured.capture_outcome == "success" and captured.terminal_events == ()
    before = [wire(e) for e in captured.captured_events]
    restarted = make_adapter(tmp_path)
    restarted.set_message_handler(FixtureRunner(no_execution).handle_message)
    redelivery = FakeMessage(valid_command)
    await restarted._handle_broker_message(redelivery)
    resolved = require_row(restarted, valid_command)
    assert redelivery.acked == 1 and resolved.outcome == "failure"
    assert resolved.failure_code == "processing_outcome_unknown"
    assert not any(e["type"] == MESSAGE_APPENDED for e in restarted._js.events)
    assert [wire(e) for e in resolved.captured_events] == before
    await replay_exact(tmp_path, valid_command, [wire(e) for e in (*resolved.started_events, *resolved.terminal_events)])
    if boundary == "before_processing":
        resume.set()
        await asyncio.wait_for(task, 2)
        assert message.nacked == 1 and message.acked == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["answer", "invocation", "turn", "ack"])
async def test_native_success_outbox_replays_immutable_bytes_at_each_boundary(native, tmp_path, valid_command, stage):
    adapter = make_adapter(tmp_path)
    original_publish = adapter._js.publish
    executions = 0
    fail_type = {
        "answer": MESSAGE_APPENDED,
        "invocation": "bloodbank.agent.invocation.completed",
        "turn": "bloodbank.conversation.turn.completed",
    }.get(stage)

    async def execute(_event):
        nonlocal executions
        executions += 1
        return native_finish(native)["final_response"]

    async def publish(subject, payload, **kwargs):
        if json.loads(payload)["type"] == fail_type:
            row = require_row(adapter, valid_command)
            assert row.state == "completed" and row.outcome == "success"
            assert wire(row.terminal_events[0]) == wire(row.captured_events[0])
            raise RuntimeError("PubAck unavailable")
        await original_publish(subject, payload, **kwargs)

    adapter._js.publish = publish
    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(valid_command, fail_ack=stage == "ack")
    await adapter._handle_broker_message(message)
    row = require_row(adapter, valid_command)
    assert row.state == "completed" and row.outcome == "success"
    assert message.nacked == 1 and message.acked == 0
    assert row.terminal_events[0]["data"]["text"] == BODY
    await replay_exact(tmp_path, valid_command, [wire(e) for e in (*row.started_events, *row.terminal_events)])
    assert executions == 1


@pytest.mark.asyncio
async def test_native_capture_then_wall_clock_timeout_excludes_answer(native, tmp_path, valid_command):
    adapter = make_timeout_adapter(tmp_path, timeout=0.2)
    interactive = lane(adapter, "interactive")
    command = memo_command(valid_command, "native-finalizer-timeout")

    async def execute(_event):
        native_finish(native)
        await asyncio.Event().wait()  # Model finalized, platform has not settled.

    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(command)
    await asyncio.wait_for(adapter._handle_broker_message(message, interactive), 5)
    row = require_row(adapter, command)
    assert row.capture_outcome == "success"
    assert row.failure_code == "turn_timeout" and row.outcome == "failure"
    assert message.acked == 1
    assert not any(e["type"] == MESSAGE_APPENDED for e in adapter._js.events)
    restarted = make_timeout_adapter(tmp_path, timeout=0.2)
    restarted.set_message_handler(FixtureRunner(no_execution).handle_message)
    replay = FakeMessage(command)
    await restarted._handle_broker_message(replay, lane(restarted, "interactive"))
    assert replay.acked == 1
    assert [wire(e) for e in restarted._js.events] == [
        wire(e) for e in (*row.queued_events, *row.started_events, *row.terminal_events)
    ]


def test_native_capture_survives_abrupt_process_exit(release, tmp_path, valid_command):
    command_path = tmp_path / "command.json"
    command_path.write_text(json.dumps(valid_command))
    script = """
import asyncio, json, os, sys
from pathlib import Path
import conftest
from test_adapter import make_adapter
from test_native_outcome import native_finish
from test_hermes_seam import native
from bloodbank_hermes_gateway.contract import Invocation, started_events
from bloodbank_hermes_gateway.execution_state import envelope_digest
from bloodbank_hermes_gateway.plugin import CommandBinding, command_binding
from pytest import MonkeyPatch

async def execute():
    root, command_path, release = map(Path, sys.argv[1:])
    command = json.loads(command_path.read_text())
    adapter = make_adapter(root)
    invocation = Invocation.from_envelope(command, 'bloodbank-pm')
    digest = envelope_digest(command)
    adapter.execution_state.claim_pending(command_id=invocation.invocation_id,
        digest=digest, profile=invocation.profile, started_events=started_events(invocation))
    adapter.execution_state.mark_started(command_id=invocation.invocation_id, digest=digest)
    native_runtime = native.__wrapped__(release, MonkeyPatch(), root)
    command_binding.set(CommandBinding(invocation, digest, adapter.execution_state))
    result = native_finish(native_runtime)
    assert result['final_response']
    os._exit(73)  # No platform completion, finally cleanup, or Python shutdown.

asyncio.run(execute())
"""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root), str(root / "tests"))))
    exited = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(command_path), str(release)],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert exited.returncode == 73, exited.stderr
    from bloodbank_hermes_gateway.execution_state import ExecutionStateStore

    captured = ExecutionStateStore(tmp_path / "execution-state.sqlite3").get(valid_command["command_id"])
    assert captured is not None and captured.state == "started"
    assert captured.capture_outcome == "success" and captured.outcome is None
    assert captured.captured_events[0]["data"]["text"] == BODY

    async def recover():
        restarted = make_adapter(tmp_path)
        restarted.set_message_handler(FixtureRunner(no_execution).handle_message)
        message = FakeMessage(valid_command)
        await restarted._handle_broker_message(message)
        resolved = require_row(restarted, valid_command)
        assert message.acked == 1 and resolved.failure_code == "processing_outcome_unknown"
        assert resolved.captured_events == captured.captured_events
        assert not any(e["type"] == MESSAGE_APPENDED for e in restarted._js.events)

    asyncio.run(recover())


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    "HTTP 400: provider rejected request",
    "API call failed: provider rejected request",
    "HTTP 429: no eligible route; " + ("provider details\n" * 35),
    "API call failed after 3 retries: HTTP 429 no eligible fast route\n" + ("details\n" * 70),
])
async def test_explicit_native_failed_flag_never_completes_or_replays_answer(
    tmp_path, valid_command, reply
):
    from bloodbank_hermes_gateway.plugin import pre_llm_call, post_llm_call, on_session_end

    adapter = make_adapter(tmp_path)
    executions = 0

    async def execute(_event):
        nonlocal executions
        executions += 1
        native = dict(session_id="disposable-session", turn_id="disposable-turn",
                      task_id="disposable-task", platform="bloodbank")
        pre_llm_call(**native, parent_session_id="")
        post_llm_call(**native, assistant_response=reply)
        on_session_end(**native, completed=False, failed=True, interrupted=False,
                       turn_exit_reason="text_response(finish_reason=stop)")
        return reply

    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    row = require_row(adapter, valid_command)
    assert executions == message.acked == 1 and message.nacked == 0
    assert row.capture_outcome == row.outcome == "failure"
    assert row.captured_events == ()
    assert row.failure_code == "hermes_turn_error"
    assert row.terminal_events[0]["type"] == "bloodbank.agent.invocation.failed"
    assert row.terminal_events[0]["data"]["refusal"] == "native.failed"
    assert not any(event["type"] == MESSAGE_APPENDED for event in adapter._js.events)
    await replay_exact(tmp_path, valid_command,
                       [wire(event) for event in (*row.started_events, *row.terminal_events)])
    assert executions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    "I investigated the report. HTTP 400 was the old failure; the tool now works.",
    "HTTP 404 means the resource does not exist.",
    "API call failed yesterday; fixed and verified today.",
    "HTTP 400: This indicates an invalid request. Check the payload.",
    "HTTP429:no eligible route; this is the error we saw yesterday. The new route now works.",
    "HTTP 400: provider rejected request was the old log entry. I fixed the configuration; the bounded tool now works.",
    "API call failed: HTTP 429 was yesterday. I retried the tool and it now succeeds.",
    "HTTP 429: no eligible route; " + "provider details\n" * 40,
    "⏱️ The AI model service is rate-limiting requests. Wait a moment, then use /retry.",
    "Your request was not processed. Send it again if you still want me to carry it out.",
    "Operation interrupted: handling API error (12.3s elapsed).",
])
async def test_native_success_accepts_arbitrary_error_text(tmp_path, valid_command, reply):
    adapter = make_adapter(tmp_path)

    async def execute(_event):
        native = dict(session_id="disposable-session", turn_id="disposable-turn",
                      task_id="disposable-task", platform="bloodbank")
        from bloodbank_hermes_gateway.plugin import pre_llm_call, post_llm_call, on_session_end
        pre_llm_call(**native, parent_session_id="")
        post_llm_call(**native, assistant_response=reply)
        on_session_end(**native, completed=True, failed=False, interrupted=False,
                       turn_exit_reason="text_response(finish_reason=stop)")
        return reply

    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    row = require_row(adapter, valid_command)
    assert message.acked == 1 and message.nacked == 0
    assert row.outcome == row.capture_outcome == "success"
    assert [event["data"]["text"] for event in row.terminal_events
            if event["type"] == MESSAGE_APPENDED] == [reply]
    assert row.terminal_events[1]["type"] == "bloodbank.agent.invocation.completed"
    await replay_exact(tmp_path, valid_command,
                       [wire(event) for event in (*row.started_events, *row.terminal_events)])


@pytest.mark.asyncio
async def test_native_success_quoting_interrupt_during_drain_stays_success(
    tmp_path, valid_command
):
    adapter = make_adapter(tmp_path)
    runner = stopping_runner(adapter)
    reply = "Operation interrupted: handling API error (12.3s elapsed)."

    async def execute(_event):
        native = dict(session_id="disposable-session", turn_id="disposable-turn",
                      task_id="disposable-task", platform="bloodbank")
        plugin.pre_llm_call(**native, parent_session_id="")
        plugin.post_llm_call(**native, assistant_response=reply)
        plugin.on_session_end(**native, completed=True, failed=False, interrupted=False,
                              turn_exit_reason="text_response(finish_reason=stop)")
        runner.begin_stop()  # Draining, but no actual native-turn interruption.
        return reply

    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    row = require_row(adapter, valid_command)
    assert message.acked == 1 and message.nacked == 0
    assert row.outcome == row.capture_outcome == "success"
    assert row.failure_code is None
    assert row.terminal_events[0]["data"]["text"] == reply
    await replay_exact(tmp_path, valid_command,
                       [wire(e) for e in (*row.started_events, *row.terminal_events)])


@pytest.mark.asyncio
async def test_raised_processing_failure_after_native_finalizer_cannot_complete(
    tmp_path, valid_command
):
    from bloodbank_hermes_gateway.plugin import pre_llm_call, post_llm_call, on_session_end

    adapter = make_adapter(tmp_path)
    native = dict(session_id="disposable-session", turn_id="disposable-turn",
                  task_id="disposable-task", platform="bloodbank")

    async def execute(_event):
        pre_llm_call(**native, parent_session_id="")
        post_llm_call(**native, assistant_response=BODY)
        on_session_end(**native, completed=True, failed=False, interrupted=False,
                       turn_exit_reason="text_response(finish_reason=stop)")
        raise RuntimeError("synthetic failure after native finalization")

    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    row = require_row(adapter, valid_command)
    assert message.acked == 1 and row.capture_outcome == "success"
    assert row.outcome == "failure" and row.captured_events[0]["data"]["text"] == BODY
    assert not any(event["type"] == MESSAGE_APPENDED for event in adapter._js.events)
    assert [event["type"] for event in row.terminal_events] == [
        "bloodbank.agent.invocation.failed", "bloodbank.conversation.turn.completed"
    ]
    await replay_exact(tmp_path, valid_command,
                       [wire(event) for event in (*row.started_events, *row.terminal_events)])
