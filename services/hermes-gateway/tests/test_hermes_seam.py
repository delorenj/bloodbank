from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import os
import re
import subprocess
import sys
import threading
import types
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from bloodbank_hermes_gateway import plugin
from bloodbank_hermes_gateway.contract import MESSAGE_APPENDED
from test_adapter import FakeMessage, make_adapter

PIN = "0408fec7a153e6c32c064acd2b8053917f1525f1"


@pytest.fixture
def release():
    path = os.environ.get("BB30_HERMES_RELEASE")
    if not path:
        pytest.skip("native proof requires BB30_HERMES_RELEASE")
    return Path(path)


def source(release, relative):
    return subprocess.run(
        ["git", "show", f"{PIN}:{relative}"], cwd=release,
        check=True, capture_output=True, text=True,
    ).stdout


def compile_nodes(release, relative, names, namespace, *, owner=None):
    tree = ast.parse(source(release, relative))
    nodes = tree.body
    if owner:
        nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == owner).body
    selected = [n for n in nodes if getattr(n, "name", None) in names]
    assert len(selected) == len(names)
    exec(compile(ast.Module(body=selected, type_ignores=[]), relative, "exec"), namespace)
    return namespace


def hook_expression(release, relative, name):
    calls = [n for n in ast.walk(ast.parse(source(release, relative)))
             if isinstance(n, ast.Call) and n.args
             and isinstance(n.args[0], ast.Constant) and n.args[0].value == name]
    assert len(calls) == 1
    return compile(ast.Expression(body=calls[0]), relative, "eval")


@contextmanager
def home_scope(home, path):
    token = home.set(str(path))
    try:
        yield
    finally:
        home.reset(token)


@pytest.fixture
def native(release, monkeypatch, tmp_path):
    home = ContextVar("native-home", default=str(tmp_path / "host"))
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: Path(home.get())
    constants.set_hermes_home_override = home.set
    constants.reset_hermes_home_override = home.reset
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    ns = {
        "Any": Any, "Callable": Callable, "Dict": dict, "List": list, "Optional": Optional,
        "Path": Path, "inspect": inspect, "threading": threading,
        "logger": logging.getLogger("native-test"), "OBSERVER_SCHEMA_VERSION": "1",
        "VALID_HOOKS": {"pre_llm_call", "post_llm_call", "on_session_end"},
        "get_hermes_home": constants.get_hermes_home, "hermes_home_key": str,
        "_plugin_manager": None, "_plugin_managers_by_home": {},
        "_plugin_managers_lock": threading.RLock(), "_join_background_discovery": lambda: None,
    }
    methods = compile_nodes(release, "hermes_cli/plugins.py",
                            {"invoke_hook", "_invoke_hook_callback", "iter_hook_callbacks"},
                            dict(ns), owner="PluginManager")

    class Manager:
        invoke_hook = methods["invoke_hook"]
        _invoke_hook_callback = methods["_invoke_hook_callback"]
        iter_hook_callbacks = methods["iter_hook_callbacks"]

        def __init__(self, scope_key):
            self.scope_key = scope_key
            self._hooks = {}
            self._discovered = False

        def discover_and_load(self):
            self._discovered = True

    ns["PluginManager"] = Manager
    compile_nodes(release, "hermes_cli/plugins.py",
                  {"_plugin_home_key", "get_plugin_manager", "_delivery_manager", "invoke_hook"}, ns)
    registration = compile_nodes(release, "hermes_cli/plugins.py", {"register_hook"},
                                 dict(ns, PluginRegistration=Any), owner="PluginContext")

    class Context:
        register_hook = registration["register_hook"]

        def __init__(self, manager):
            self._manager = manager
            self.manifest = SimpleNamespace(name="bloodbank-platform")

        def _track(self, *_args):
            return SimpleNamespace(active=True)

    def register():
        manager = ns["get_plugin_manager"]()
        plugin.register_hooks(Context(manager))
        return manager

    plugins = types.ModuleType("hermes_cli.plugins")
    plugins.get_plugin_manager = ns["get_plugin_manager"]
    plugins.discover_plugins = lambda: ns["_delivery_manager"]()
    plugins.invoke_hook = ns["invoke_hook"]
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    monkeypatch.setattr(sys.modules["hermes_cli"], "plugins", plugins, raising=False)
    lifecycle = types.ModuleType("hermes_cli.lifecycle")
    lifecycle.invoke_hook = ns["invoke_hook"]
    monkeypatch.setitem(sys.modules, "hermes_cli.lifecycle", lifecycle)
    loop = types.ModuleType("agent.conversation_loop")
    loop.logger = ns["logger"]
    loop._notify_context_engine_turn_complete = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "agent.conversation_loop", loop)
    filters = dict(re=re)
    tree = ast.parse(source(release, "gateway/response_filters.py"))
    exec(compile(tree, "native-filters", "exec"), filters)
    monkeypatch.setattr(plugin, "is_intentional_silence_response", filters["is_intentional_silence_response"])
    final_ns = dict(os=os, flatten_message_text=lambda value: value or "",
                    append_message=lambda messages, message: messages.append(message),
                    stamp_message_timestamp=lambda *_args: None,
                    _summarize_user_message_for_log=str,
                    _VERIFICATION_CONTINUATION_FLAGS=("_verification_stop_synthetic", "_pre_verify_synthetic"))
    sanitizer = compile_nodes(release, "agent/message_sanitization.py", {"_sanitize_surrogates"},
                              dict(_SURROGATE_RE=re.compile(r"[\ud800-\udfff]")))
    final_ns["_sanitize_surrogates"] = sanitizer["_sanitize_surrogates"]
    compile_nodes(release, "agent/turn_finalizer.py",
                  {"finalize_turn", "_is_pure_tool_call_tail", "_drop_verification_continuation_scaffolding"}, final_ns)
    executor = compile_nodes(release, "gateway/run.py", {"_run_in_executor_with_context"},
                             dict(asyncio=asyncio, copy_context=copy_context), owner="GatewayRunner")
    hermes_profiles = sys.modules["hermes_cli.profiles"]
    monkeypatch.setattr(hermes_profiles, "get_profile_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(sys.modules["gateway.run"], "_profile_runtime_scope", lambda path: home_scope(home, path))
    with home_scope(home, tmp_path / "bloodbank-pm"):
        register()
    register()
    return SimpleNamespace(home=home, constants=constants, register=register, ns=ns,
                           finalize=final_ns["finalize_turn"],
                           pre=hook_expression(release, "agent/turn_context.py", "pre_llm_call"),
                           executor=executor["_run_in_executor_with_context"])


def agent_fixture():
    agent = SimpleNamespace(
        session_id="native-session", model="benign-model", provider="automaticai",
        platform="bloodbank", base_url="fixture", max_iterations=50,
        iteration_budget=SimpleNamespace(remaining=40, used=10, max_total=50),
        context_compressor=None, _tool_guardrail_halt_decision=None, quiet_mode=True,
        _skill_nudge_interval=0, _iters_since_skill=0, valid_tool_names=[],
        _parent_session_id=None, _user_id="issuer", skip_background_review=True,
        session_estimated_cost_usd=0, session_cost_status="fixture", session_cost_source="fixture",
    )
    for key in ("input", "output", "cache_read", "cache_write", "reasoning", "prompt", "completion", "total"):
        setattr(agent, f"session_{key}_tokens", 0)
    for method in ("_save_trajectory", "_cleanup_task_resources", "_persist_session",
                   "_drop_trailing_empty_response_scaffolding", "clear_interrupt",
                   "_sync_external_memory_for_turn"):
        setattr(agent, method, lambda *_args, **_kwargs: None)
    agent._drain_pending_steer = list
    agent._turn_completion_explainer_enabled = lambda: True
    agent._format_turn_completion_explanation = lambda *_args: "Benign diagnostic explanation."
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["normal", "surrogate", "reasoning", "empty", "partial", "history", "failed", "interrupted"])
async def test_full_pinned_finalizer_native_provenance(native, release, tmp_path, valid_command, case):
    adapter = make_adapter(tmp_path)
    returned = []
    body = "  é Full benign final\n" * 80 + "\n尾\n"
    reason = "text_response(finish_reason=stop)"
    if case == "reasoning":
        tree = ast.parse(source(release, "agent/conversation_loop.py"))
        assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                          and n.lineno == 7616)
        scope = dict(agent=SimpleNamespace(_fallback_chain=[]), reasoning_preview="INTERNAL-REASONING-MARKER")
        exec(compile(ast.Module(body=[assignment], type_ignores=[]), "native-reasoning", "exec"), scope)
        body = scope["final_response"]
        reason = "empty_response_exhausted"
    elif case == "empty":
        body, reason = "(empty)", "empty_response_exhausted"
    elif case == "partial":
        body, reason = "The", "partial_stream_recovery"
    elif case == "history":
        body, reason = "Prior answer must not become current", "fallback_prior_turn_content"

    async def handler(_event):
        agent = agent_fixture()
        params = dict(agent=agent, effective_task_id="native-task", turn_id="native-turn",
                      original_user_message="benign prompt", messages=[], conversation_history=[],
                      _invoke_hook=native.ns["invoke_hook"])
        eval(native.pre, dict(params))
        if case == "surrogate":
            native.ns["get_plugin_manager"]()._hooks["transform_llm_output"] = [lambda **_kw: body + "\ud800 tail"]
        elif case in {"reasoning", "empty", "partial", "history"}:
            native.ns["get_plugin_manager"]()._hooks["transform_llm_output"] = [lambda **kw: "Transformed diagnostic: " + kw["response_text"]]
        params.pop("_invoke_hook")
        result = native.finalize(**params, final_response=body, api_call_count=10,
                                 interrupted=case == "interrupted", failed=case == "failed",
                                 user_message="benign prompt", _should_review_memory=False,
                                 _turn_exit_reason=reason)
        returned.append(result)
        return result["final_response"]

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    assert message.acked == 1
    answers = [e for e in adapter._js.events if e["type"] == MESSAGE_APPENDED]
    if case in {"normal", "surrogate"}:
        assert len(answers) == 1
        assert answers[0]["data"]["text"] == returned[0]["final_response"]
        assert len(answers[0]["data"]["text"]) > 500
        assert answers[0]["data"]["text"].encode("utf-8")
    else:
        assert not answers
    if case in {"reasoning", "empty", "partial", "history"}:
        assert returned[0]["completed"] is True


@pytest.mark.asyncio
async def test_actual_native_callback_payloads_and_executor_context(native, tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    returned = []

    async def handler(_event):
        agent = agent_fixture()
        binding = plugin.command_binding.get()

        def work():
            assert plugin.command_binding.get() is binding
            params = dict(agent=agent, effective_task_id="actual-task", turn_id="actual-turn",
                          original_user_message="benign prompt", messages=[], conversation_history=[])
            eval(native.pre, dict(params, _invoke_hook=native.ns["invoke_hook"]))
            result = native.finalize(**params, final_response="é Native final\n" * 100,
                                     api_call_count=10, interrupted=False, failed=False,
                                     user_message="benign prompt", _should_review_memory=False,
                                     _turn_exit_reason="text_response(finish_reason=stop)")
            returned.append(result)

        with ThreadPoolExecutor(max_workers=1) as executor:
            await native.executor(SimpleNamespace(_get_executor=lambda: executor), work)
        return "receipt"

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    assert message.acked == 1
    answer = next(e for e in adapter._js.events if e["type"] == MESSAGE_APPENDED)
    assert answer["data"]["text"] == returned[0]["final_response"]


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [True, False])
async def test_home_scoped_manager_capture_preflight(native, tmp_path, valid_command, supported):
    from conftest import FixtureRunner

    adapter = make_adapter(tmp_path, target_profiles={"bloodbank-pm": "research"})
    host_manager = native.ns["get_plugin_manager"]()
    with home_scope(native.home, tmp_path / "research"):
        target_manager = native.ns["get_plugin_manager"]()
        assert target_manager is not host_manager
        if supported:
            native.register()
        else:
            target_manager._hooks["post_llm_call"] = [lambda session_id: None]
    executions = []

    async def execute(_event):
        executions.append(native.ns["get_plugin_manager"]())
        agent = agent_fixture()
        params = dict(agent=agent, effective_task_id="profile-task", turn_id="profile-turn",
                      original_user_message="benign prompt", messages=[], conversation_history=[])
        eval(native.pre, dict(params, _invoke_hook=native.ns["invoke_hook"]))
        native.ns["invoke_hook"]("token", assistant_response="interim")
        child = dict(session_id="child-session", turn_id="child-turn", task_id="child-task",
                     platform="bloodbank", parent_session_id="native-session")
        native.ns["invoke_hook"]("pre_llm_call", **child)
        native.ns["invoke_hook"]("post_llm_call", **child, assistant_response="child answer")
        native.ns["invoke_hook"]("on_session_end", **child, completed=True, failed=False,
                                  interrupted=False, turn_exit_reason="text_response(finish_reason=stop)")
        result = native.finalize(**params, final_response="  é Profile final\n" * 80,
                                 api_call_count=10, interrupted=False, failed=False,
                                 user_message="benign prompt", _should_review_memory=False,
                                 _turn_exit_reason="text_response(finish_reason=stop)")
        return result["final_response"]

    runner = FixtureRunner(execute)
    adapter.set_message_handler(runner.handle_message)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    answers = [e for e in adapter._js.events if e["type"] == MESSAGE_APPENDED]
    if supported:
        assert executions == [target_manager]
        assert message.acked == 1 and len(answers) == 1
        assert answers[0]["data"]["profile_name"] == "research"
        assert answers[0]["data"]["text"] == "  é Profile final\n" * 80
    else:
        assert executions == [] and not answers
        assert message.termed == 1 and message.acked == 0
        stored = adapter.execution_state.get(valid_command["command_id"])
        assert stored.state == "rejected_closed"
        assert stored.terminal_events[0]["data"]["error_code"] == plugin.CaptureUnavailable.reason
        replay = FakeMessage(valid_command)
        await adapter._handle_broker_message(replay)
        assert replay.termed == 1 and executions == []
    assert native.ns["get_plugin_manager"]() is host_manager


@pytest.mark.asyncio
async def test_full_pinned_codex_runtime_has_no_hooks_and_is_rejected_before_execution(
    native, release, monkeypatch, tmp_path, valid_command
):
    from conftest import FixtureRunner

    session_module = types.ModuleType("agent.transports.codex_app_server_session")
    session_module.CodexAppServerSession = lambda **_kwargs: pytest.fail("no subprocess")
    session_module._ServerRequestRouting = SimpleNamespace
    monkeypatch.setitem(sys.modules, "agent.transports.codex_app_server_session", session_module)
    namespace = dict(Any=Any, Dict=dict, List=list, logger=logging.getLogger("codex-native"),
                     _record_codex_app_server_compaction=lambda *_args: None,
                     _record_codex_app_server_usage=lambda *_args: {})
    compile_nodes(release, "agent/codex_runtime.py", {"run_codex_app_server_turn"}, namespace)
    calls = []
    turn = SimpleNamespace(final_text="é Benign alternate-runtime answer\n" * 80,
                           interrupted=False, error=None, projected_messages=[], tool_iterations=0,
                           thread_id="native-codex-thread", turn_id="native-codex-turn")
    agent = agent_fixture()
    agent._codex_session = SimpleNamespace(run_turn=lambda **_kw: calls.append(True) or turn)
    result = namespace["run_codex_app_server_turn"](
        agent, user_message="benign prompt", original_user_message="benign prompt",
        messages=[], effective_task_id="native-task",
    )
    assert result["completed"] is True and result["final_response"] == turn.final_text
    assert plugin.command_binding.get() is None
    calls.clear()
    adapter = make_adapter(tmp_path)

    async def execute(_event):
        return namespace["run_codex_app_server_turn"](
            agent, user_message="benign prompt", original_user_message="benign prompt",
            messages=[], effective_task_id="native-task",
        )["final_response"]

    runner = FixtureRunner(execute)
    runner._resolve_session_agent_runtime = lambda **_kw: ("fixture-model", {"api_mode": "codex_app_server"})
    adapter.set_message_handler(runner.handle_message)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    assert calls == [] and message.termed == 1 and message.acked == 0
    assert not any(e["type"] == MESSAGE_APPENDED for e in adapter._js.events)
    stored = adapter.execution_state.get(valid_command["command_id"])
    assert stored.state == "rejected_closed"
    assert stored.terminal_events[0]["type"] == "bloodbank.agent.invocation.failed"
    assert stored.terminal_events[0]["data"]["error_code"] == plugin.CaptureUnavailable.reason
