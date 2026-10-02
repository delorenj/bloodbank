"""Optional installed-release seam proof; executes only extracted stdlib callbacks.

BB30_HERMES_RELEASE points to read-only Hermes source. No agent construction,
model calls, gateway start or runtime journal access occurs.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from bloodbank_hermes_gateway import plugin
from bloodbank_hermes_gateway.contract import MESSAGE_APPENDED
from test_adapter import FakeMessage, make_adapter


@pytest.fixture
def release():
    path = os.environ.get("BB30_HERMES_RELEASE")
    if not path:
        pytest.skip("installed Hermes seam proof requires BB30_HERMES_RELEASE")
    return Path(path)


def hook_expression(release, relative_path, hook_name):
    tree = ast.parse((release / relative_path).read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == hook_name
    ]
    assert len(calls) == 1
    return compile(ast.Expression(body=calls[0]), str(release / relative_path), "eval")


@pytest.mark.asyncio
async def test_actual_native_callback_payloads_and_executor_context(
    release, tmp_path, valid_command, monkeypatch
):
    filter_path = release / "gateway/response_filters.py"
    spec = importlib.util.spec_from_file_location(
        "bb30_native_response_filters", filter_path
    )
    filters = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(filters)
    monkeypatch.setattr(
        plugin,
        "is_intentional_silence_response",
        filters.is_intentional_silence_response,
    )
    for text in [
        "[SILENT]",
        "SILENT",
        "no reply",
        "*NO_REPLY*",
        ".NO_REPLY",
        "  No  Reply  ",
    ]:
        assert filters.is_intentional_silence_response(text)
    assert not filters.is_intentional_silence_response(
        "The text NO_REPLY is a control token."
    )

    pre = hook_expression(release, "agent/turn_context.py", "pre_llm_call")
    post = hook_expression(release, "agent/turn_finalizer.py", "post_llm_call")
    end = hook_expression(release, "agent/turn_finalizer.py", "on_session_end")
    runner = ast.parse((release / "gateway/run.py").read_text())
    helper = next(
        node
        for node in ast.walk(runner)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_run_in_executor_with_context"
    )
    namespace = {
        "asyncio": asyncio,
        "copy_context": __import__("contextvars").copy_context,
    }
    exec(  # noqa: S102 - only the read-only pinned stdlib executor helper
        compile(
            ast.Module(body=[helper], type_ignores=[]),
            "native-gateway-executor-seam",
            "exec",
        ),
        namespace,
    )
    adapter = make_adapter(tmp_path)
    body = "é Native final answer\n" * 100
    seen = []

    def invoke(name, **payload):
        seen.append(name)
        return getattr(plugin, name)(**payload)

    async def handler(_event):
        params = {
            "_invoke_hook": invoke,
            "agent": SimpleNamespace(
                session_id="actual-session",
                model="fixture-model",
                platform="bloodbank",
                _parent_session_id=None,
                _user_id="issuer",
            ),
            "effective_task_id": "actual-task",
            "turn_id": "actual-turn",
            "original_user_message": "fixture prompt",
            "messages": [{"role": "tool", "content": "not an answer"}],
            "conversation_history": [],
            "final_response": body,
            "completed": True,
            "failed": False,
            "interrupted": False,
            "_turn_exit_reason": "text_response(stop)",
        }
        binding = plugin.command_binding.get()

        def work():
            assert plugin.command_binding.get() is binding
            for expression in [pre, post, end]:
                eval(expression, params)

        with ThreadPoolExecutor(max_workers=1) as executor:
            owner = SimpleNamespace(_get_executor=lambda: executor)
            await namespace["_run_in_executor_with_context"](owner, work)
        return "platform-send is not the full source"

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    assert message.acked == 1
    assert seen == ["pre_llm_call", "post_llm_call", "on_session_end"]
    answer = next(e for e in adapter._js.events if e["type"] == MESSAGE_APPENDED)
    assert answer["data"]["text"] == body
    assert answer["data"]["native_session_id"] == "actual-session"
    assert answer["data"]["native_turn_id"] == "actual-turn"
    assert answer["data"]["thread_id"] == "thread-1"
