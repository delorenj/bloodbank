from __future__ import annotations

import asyncio
import copy
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest
from bloodbank_hermes_gateway.contract import (
    MESSAGE_APPENDED,
    Invocation,
    final_answer_event,
    validate_fact,
)
from bloodbank_hermes_gateway.plugin import (
    command_binding,
    on_session_end,
    post_llm_call,
    pre_llm_call,
)
from test_adapter import FakeMessage, make_adapter, write_registry

BODY = (
    "  Full answer: café 🐦\n" + ("Do not truncate this paragraph.\n" * 40) + "\n尾\n"
)
NATIVE = {
    "session_id": "hermes-native-session",
    "turn_id": "hermes-native-turn",
    "task_id": "hermes-native-task",
    "platform": "bloodbank",
}


def capture(body=BODY, *, native=None, completed=True, failed=False, interrupted=False):
    native = native or NATIVE
    pre_llm_call(**native, parent_session_id="")
    post_llm_call(
        **native,
        assistant_response=body,
        conversation_history=[
            {
                "role": "assistant",
                "content": "old answer",
                "reasoning": "hidden reasoning",
            },
            {"role": "tool", "content": "tool output"},
        ],
    )
    on_session_end(
        **native, completed=completed, failed=failed, interrupted=interrupted
    )


def wire(event):
    return json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")


@pytest.mark.asyncio
async def test_full_answer_capture_precedes_publish_and_preserves_exact_lineage(
    tmp_path, valid_command
):
    valid_command["causationid"] = str(uuid.uuid4())
    valid_command["actor"]["provider"] = "issuer-provider"
    valid_command["data"]["thread_id"] = " logical thread "
    adapter = make_adapter(tmp_path)
    original_publish = adapter._js.publish
    message = FakeMessage(valid_command)
    observed = []

    async def publish(subject, payload, **kwargs):
        fact = json.loads(payload)
        if fact["type"] == MESSAGE_APPENDED:
            stored = adapter.execution_state.get(valid_command["command_id"])
            assert stored.state == "completed"
            assert wire(stored.terminal_events[0]) == payload
            assert message.acked == 0
            observed.append(fact)
        await original_publish(subject, payload, **kwargs)

    adapter._js.publish = publish

    async def handler(event):
        assert event.source.profile == "bloodbank-pm"
        capture()
        # Delivery sends are deliberately not the full-answer source.
        await adapter.send(event.source.chat_id, "interim status")
        return BODY[:500]

    adapter.set_message_handler(handler)
    await adapter._handle_broker_message(message)
    assert message.acked == 1 and message.nacked == 0
    assert len(observed) == 1
    answer = observed[0]
    validate_fact(answer)
    data = answer["data"]
    assert data["text"].encode("utf-8") == BODY.encode("utf-8")
    assert len(data["text"]) > 500
    assert data["role"] == "assistant" and data["final_answer"] is True
    assert answer["correlationid"] == valid_command["correlationid"]
    assert answer["causationid"] == valid_command["id"]
    assert data["command_causationid"] == valid_command["causationid"]
    assert data["issuer"] == valid_command["actor"]
    assert answer["actor"]["agent_id"] == valid_command["data"]["target_agent_id"]
    for key in ["command_id", "idempotency_key"]:
        assert data[key] == valid_command[key]
    for key in ["thread_id", "turn_id", "target_agent_id"]:
        assert data[key] == valid_command["data"][key]
    assert data["profile_name"] == "bloodbank-pm"
    assert data["native_session_id"] == NATIVE["session_id"]
    assert data["native_turn_id"] == NATIVE["turn_id"]
    assert data["native_session_id"] != data["thread_id"]
    assert data["command_event_id"] == valid_command["id"]
    assert [e["type"] for e in adapter._js.events] == [
        "bloodbank.conversation.turn.started",
        "bloodbank.agent.invocation.started",
        MESSAGE_APPENDED,
        "bloodbank.agent.invocation.completed",
        "bloodbank.conversation.turn.completed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,flags",
    [
        ("", {}),
        ("  \n", {}),
        (None, {}),
        ("[SILENT]", {}),
        ("NO_REPLY", {}),
        ("SILENT", {}),
        ("no reply", {}),
        (BODY, {"failed": True}),
        (BODY, {"completed": False}),
        (BODY, {"interrupted": True}),
    ],
)
async def test_empty_silent_failed_interrupted_never_answer(
    tmp_path, valid_command, body, flags
):
    adapter = make_adapter(tmp_path)

    async def handler(_event):
        capture(body, **flags)
        return "local receipt"

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    assert message.acked == 1
    assert not any(e["type"] == MESSAGE_APPENDED for e in adapter._js.events)
    stored = adapter.execution_state.get(valid_command["command_id"])
    assert stored.outcome == (
        "cancelled" if flags.get("interrupted") else "failure" if flags else "success"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "send",
        "history",
        "post_without_pre",
        "post_without_end",
        "cancelled",
        "failed",
        "rejected",
    ],
)
async def test_unqualified_output_never_answer(tmp_path, valid_command, mode):
    adapter = make_adapter(tmp_path)

    async def handler(event):
        if mode == "send":
            await adapter.send(
                event.source.chat_id,
                BODY,
                metadata={"role": "assistant", "final_answer": True},
            )
        elif mode == "history":
            pre_llm_call(
                **NATIVE,
                parent_session_id="",
                conversation_history=[{"role": "assistant", "content": BODY}],
            )
            on_session_end(**NATIVE, completed=True, failed=False, interrupted=False)
        elif mode == "post_without_pre":
            post_llm_call(**NATIVE, assistant_response=BODY)
        elif mode == "post_without_end":
            pre_llm_call(**NATIVE, parent_session_id="")
            post_llm_call(**NATIVE, assistant_response=BODY)
        elif mode == "cancelled":
            raise asyncio.CancelledError
        elif mode == "failed":
            raise RuntimeError("Hermes failure")
        return BODY

    if mode == "rejected":
        write_registry(tmp_path)
        valid_command["data"]["target_agent_id"] = "not-routable"
    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    assert not any(e["type"] == MESSAGE_APPENDED for e in adapter._js.events)
    assert message.termed == (1 if mode == "rejected" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["answer", "terminal", "turn", "ack"])
async def test_publish_ack_failure_restart_replays_exact_bytes_without_execution(
    tmp_path, valid_command, stage
):
    first = make_adapter(tmp_path)
    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1
        capture()
        return "local send"

    first.set_message_handler(handler)
    original_publish = first._js.publish
    fail_type = {
        "answer": MESSAGE_APPENDED,
        "terminal": "bloodbank.agent.invocation.completed",
        "turn": "bloodbank.conversation.turn.completed",
    }.get(stage)

    async def fail_publish(subject, payload, **kwargs):
        if json.loads(payload)["type"] == fail_type:
            raise RuntimeError("PubAck unavailable")
        await original_publish(subject, payload, **kwargs)

    first._js.publish = fail_publish
    original = FakeMessage(valid_command, fail_ack=stage == "ack")
    await first._handle_broker_message(original)
    assert original.acked == 0 and original.nacked == 1
    stored = first.execution_state.get(valid_command["command_id"])
    assert stored.state == "completed"
    expected = [wire(e) for e in (*stored.started_events, *stored.terminal_events)]
    assert stored.terminal_events[0]["data"]["text"] == BODY

    for _ in range(2):
        restarted = make_adapter(tmp_path)
        restarted.set_message_handler(handler)
        redelivery = FakeMessage(valid_command)
        await restarted._handle_broker_message(redelivery)
        assert redelivery.acked == 1
        assert [wire(e) for e in restarted._js.events] == expected
    assert executions == 1


@pytest.mark.asyncio
async def test_puback_for_answer_and_each_terminal_blocks_command_ack(
    tmp_path, valid_command
):
    adapter = make_adapter(tmp_path)
    gate = asyncio.Queue()
    release = asyncio.Queue()
    message = FakeMessage(valid_command)
    original = adapter._js.publish

    async def publish(subject, payload, **kwargs):
        fact = json.loads(payload)
        if fact["type"] in {
            MESSAGE_APPENDED,
            "bloodbank.agent.invocation.completed",
            "bloodbank.conversation.turn.completed",
        }:
            await gate.put(fact["type"])
            await release.get()
        await original(subject, payload, **kwargs)

    adapter._js.publish = publish

    async def handler(_event):
        capture()
        return "receipt"

    adapter.set_message_handler(handler)
    task = asyncio.create_task(adapter._handle_broker_message(message))
    for expected in [
        MESSAGE_APPENDED,
        "bloodbank.agent.invocation.completed",
        "bloodbank.conversation.turn.completed",
    ]:
        assert await asyncio.wait_for(gate.get(), 2) == expected
        assert message.acked == 0
        await release.put(True)
    await asyncio.wait_for(task, 2)
    assert message.acked == 1


@pytest.mark.asyncio
async def test_capture_failure_is_sticky_despite_hermes_swallowing_hook_errors(
    tmp_path, valid_command
):
    adapter = make_adapter(tmp_path)
    mark = adapter.execution_state.mark_completed
    invoked = 0

    def fail_capture(**_kwargs):
        raise OSError("disk unavailable before capture")

    adapter.execution_state.mark_completed = fail_capture

    async def handler(_event):
        nonlocal invoked
        invoked += 1
        try:
            capture()
        except OSError:
            pass  # Exact Hermes observer exception policy.
        return "receipt"

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    assert message.nacked == 1 and message.acked == 0
    assert adapter.execution_state.get(valid_command["command_id"]).state == "started"
    assert not any(e["type"] == MESSAGE_APPENDED for e in adapter._js.events)
    # This is pre-capture ambiguity: retry can execute Hermes again.
    adapter.execution_state.mark_completed = mark
    retry = FakeMessage(valid_command)
    await adapter._handle_broker_message(retry)
    assert invoked == 2 and retry.acked == 1


@pytest.mark.asyncio
async def test_restart_after_capture_before_processing_callback_never_reinvokes(
    tmp_path, valid_command
):
    first = make_adapter(tmp_path)
    captured = asyncio.Event()
    pause = asyncio.Event()

    async def handler(_event):
        capture()
        captured.set()
        await pause.wait()
        return "receipt"

    first.set_message_handler(handler)
    message = FakeMessage(valid_command)
    task = asyncio.create_task(first._handle_broker_message(message))
    await asyncio.wait_for(captured.wait(), 2)
    stored = first.execution_state.get(valid_command["command_id"])
    assert stored.state == "completed" and len(first._js.events) == 2
    restarted = make_adapter(tmp_path)

    async def forbidden(_event):
        raise AssertionError("post-capture execution")

    restarted.set_message_handler(forbidden)
    replay = FakeMessage(valid_command)
    await restarted._handle_broker_message(replay)
    assert replay.acked == 1
    assert [wire(e) for e in restarted._js.events] == [
        wire(e) for e in (*stored.started_events, *stored.terminal_events)
    ]
    pause.set()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_duplicate_capture_immutable_conflict_fails_closed(
    tmp_path, valid_command
):
    adapter = make_adapter(tmp_path)

    async def handler(_event):
        capture()
        stored = command_binding.get().result
        capture()
        assert command_binding.get().result == stored
        try:
            post_llm_call(**NATIVE, assistant_response="conflicting second body")
        except RuntimeError:
            pass
        return "receipt"

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    assert message.acked == 0 and message.nacked == 1
    stored = adapter.execution_state.get(valid_command["command_id"])
    original_bytes = [wire(e) for e in stored.terminal_events]
    assert stored.terminal_events[0]["data"]["text"] == BODY
    changed = copy.deepcopy(stored.terminal_events)
    changed[0]["data"]["text"] = "replacement"
    with pytest.raises(RuntimeError, match="immutable"):
        adapter.execution_state.mark_completed(
            command_id=stored.command_id,
            digest=stored.envelope_digest,
            outcome="success",
            terminal_events=changed,
        )
    assert [
        wire(e) for e in adapter.execution_state.get(stored.command_id).terminal_events
    ] == original_bytes


@pytest.mark.asyncio
async def test_profile_isolation_child_exclusion_and_real_execution_thread_context(
    tmp_path, valid_command
):
    adapter = make_adapter(
        tmp_path, target_profiles={"bloodbank-pm": "research", "other": "operations"}
    )
    second = copy.deepcopy(valid_command)
    second.update(
        command_id=str(uuid.uuid4()),
        id=str(uuid.uuid4()),
        correlationid=str(uuid.uuid4()),
        idempotency_key="second-command",
    )
    second["data"]["target_agent_id"] = "other"
    both = asyncio.Event()
    entered = []
    thread_bindings = []

    async def handler(event):
        entered.append(event.source.profile)
        if len(entered) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 2)
        profile = event.source.profile
        native = dict(
            NATIVE,
            session_id=f"native-{profile}",
            turn_id=f"turn-{profile}",
            task_id=f"task-{profile}",
        )

        def execute():
            binding = command_binding.get()
            thread_bindings.append(binding.invocation.profile)
            pre_llm_call(**native, parent_session_id="")
            child = dict(
                NATIVE,
                session_id=f"child-{profile}",
                turn_id="child-turn",
                task_id="child-task",
            )
            pre_llm_call(**child, parent_session_id=native["session_id"])
            post_llm_call(**child, assistant_response="child answer must be excluded")
            on_session_end(**child, completed=True, failed=False, interrupted=False)
            capture(f"{profile}\n{BODY}", native=native)

        # Same execution-thread transfer as pinned GatewayRunner's public helper.
        with ThreadPoolExecutor(max_workers=1) as executor:
            await asyncio.get_running_loop().run_in_executor(
                executor, copy_context().run, execute
            )
        assert command_binding.get().invocation.profile == profile
        return "local receipt"

    adapter.set_message_handler(handler)
    messages = [FakeMessage(valid_command), FakeMessage(second)]
    await asyncio.wait_for(
        asyncio.gather(*(adapter._handle_broker_message(m) for m in messages)), 4
    )
    assert all(m.acked == 1 for m in messages)
    assert set(thread_bindings) == {"research", "operations"}
    assert command_binding.get() is None
    answers = [e for e in adapter._js.events if e["type"] == MESSAGE_APPENDED]
    assert len(answers) == 2
    for command, profile in [(valid_command, "research"), (second, "operations")]:
        answer = next(
            e for e in answers if e["data"]["command_id"] == command["command_id"]
        )
        assert answer["data"]["text"] == f"{profile}\n{BODY}"
        assert answer["data"]["profile_name"] == profile
        assert answer["data"]["native_session_id"] == f"native-{profile}"
        assert answer["data"]["native_turn_id"] == f"turn-{profile}"
        assert answer["data"]["target_agent_id"] == command["data"]["target_agent_id"]
        assert answer["data"]["issuer"] == command["actor"]
        assert answer["correlationid"] == command["correlationid"]
        assert answer["causationid"] == command["id"]
    assert not adapter._conversation_locks


@pytest.mark.asyncio
async def test_same_profile_thread_serializes_commands_and_isolates_finals(
    tmp_path, valid_command
):
    adapter = make_adapter(tmp_path)
    second = copy.deepcopy(valid_command)
    second.update(
        command_id=str(uuid.uuid4()), id=str(uuid.uuid4()), idempotency_key="next-turn"
    )
    second["data"]["turn_id"] = "turn-2"
    started = asyncio.Event()
    release = asyncio.Event()
    order = []

    async def handler(event):
        order.append(event.message_id)
        if event.message_id == valid_command["command_id"]:
            started.set()
            await release.wait()
        capture(event.message_id + BODY, native=dict(NATIVE, turn_id=event.message_id))
        return "receipt"

    adapter.set_message_handler(handler)
    first = asyncio.create_task(
        adapter._handle_broker_message(FakeMessage(valid_command))
    )
    await asyncio.wait_for(started.wait(), 2)
    waiting = asyncio.create_task(adapter._handle_broker_message(FakeMessage(second)))
    # Wait until the second command has joined the actual lock queue.
    for _ in range(200):
        if adapter._conversation_lock_refs.get("bloodbank-pm:thread-1") == 2:
            break
        await asyncio.sleep(0.005)
    assert adapter._conversation_lock_refs["bloodbank-pm:thread-1"] == 2
    assert order == [valid_command["command_id"]]
    release.set()
    await asyncio.wait_for(asyncio.gather(first, waiting), 3)
    assert order == [valid_command["command_id"], second["command_id"]]
    answers = [e for e in adapter._js.events if e["type"] == MESSAGE_APPENDED]
    assert [e["data"]["text"] for e in answers] == [
        c["command_id"] + BODY for c in [valid_command, second]
    ]
    assert [e["data"]["turn_id"] for e in answers] == ["turn-1", "turn-2"]


def test_final_schema_is_additive_and_requires_explicit_role_body_lineage(
    valid_command,
):
    invocation = Invocation.from_envelope(valid_command, "bloodbank-pm")
    answer = final_answer_event(
        invocation,
        role="assistant",
        final_answer=True,
        text=BODY,
        native_session_id=None,
        native_turn_id=None,
    )
    again = final_answer_event(
        invocation,
        role="assistant",
        final_answer=True,
        text=BODY,
        native_session_id=None,
        native_turn_id=None,
    )
    assert answer["id"] == again["id"]
    assert answer["data"]["message_id"] == again["data"]["message_id"]
    validate_fact(answer)
    for key, value in [("role", "tool"), ("text", None), ("text", ""), ("text", " \n")]:
        malformed = copy.deepcopy(answer)
        malformed["data"][key] = value
        with pytest.raises(ValueError):
            validate_fact(malformed)
    for key in [
        "text",
        "command_id",
        "idempotency_key",
        "target_agent_id",
        "profile_name",
        "issuer",
        "command_event_id",
        "command_causationid",
    ]:
        malformed = copy.deepcopy(answer)
        del malformed["data"][key]
        with pytest.raises(ValueError):
            validate_fact(malformed)
    for role in ["user", "system", "tool", "assistant"]:
        generic = copy.deepcopy(answer)
        generic["data"] = {
            "thread_id": "thread",
            "turn_id": "turn",
            "message_id": "generic",
            "role": role,
        }
        validate_fact(generic)
        generic["data"]["text"] = None
        validate_fact(generic)
    for role, discriminator in [("user", True), ("assistant", False), ("assistant", 1)]:
        with pytest.raises(ValueError):
            final_answer_event(
                invocation,
                role=role,
                final_answer=discriminator,
                text=BODY,
                native_session_id=None,
                native_turn_id=None,
            )


@pytest.mark.asyncio
async def test_native_compaction_session_rotation_preserves_root_execution(
    tmp_path, valid_command
):
    adapter = make_adapter(tmp_path)

    async def handler(_event):
        pre_llm_call(**NATIVE, parent_session_id="")
        # Hermes compaction changes session_id, keeping execution task/turn.
        final_native = dict(NATIVE, session_id="observed-compaction-session")
        child = dict(final_native, turn_id="child-turn")
        pre_llm_call(**child, parent_session_id=NATIVE["session_id"])
        post_llm_call(**child, assistant_response="child must not claim parent")
        on_session_end(**child, completed=True, failed=False, interrupted=False)
        post_llm_call(**final_native, assistant_response=BODY)
        on_session_end(**final_native, completed=True, failed=False, interrupted=False)
        return "receipt"

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    assert message.acked == 1
    answer = next(e for e in adapter._js.events if e["type"] == MESSAGE_APPENDED)
    assert answer["data"]["text"] == BODY
    assert answer["data"]["native_session_id"] == "observed-compaction-session"
    assert answer["data"]["native_turn_id"] == NATIVE["turn_id"]
    assert answer["data"]["thread_id"] == valid_command["data"]["thread_id"]
