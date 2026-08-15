from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
import sys
import types
from types import SimpleNamespace

import pytest

from bloodbank_hermes_gateway.adapter import ROUTE_REJECTION_REASON, BloodbankAdapter
from bloodbank_hermes_gateway.contract import Invocation, started_events
from bloodbank_hermes_gateway.execution_state import (
    ExecutionStateStore,
    envelope_digest,
)


class FakeMessage:
    def __init__(self, envelope, *, fail_ack=False):
        self.data = json.dumps(envelope).encode()
        self.fail_ack = fail_ack
        self.acked = 0
        self.termed = 0
        self.nacked = 0
        self.progress = 0

    async def ack(self):
        if self.fail_ack:
            raise RuntimeError("ack transport unavailable")
        self.acked += 1

    async def term(self):
        self.termed += 1

    async def nak(self, delay=None):
        del delay
        self.nacked += 1

    async def in_progress(self):
        self.progress += 1


class FakeJetStream:
    def __init__(self):
        self.events = []

    async def publish(self, subject, payload, **kwargs):
        envelope = json.loads(payload)
        assert subject == envelope["subject"]
        assert kwargs["headers"]["Nats-Msg-Id"] == envelope["id"]
        self.events.append(envelope)


def make_adapter(tmp_path, *, target_profiles=None):
    if target_profiles is None:
        target_profiles = {"bloodbank-pm": "bloodbank-pm"}
    config = SimpleNamespace(
        enabled=True,
        extra={
            "target_profiles": target_profiles,
            "fleet_registry": str(tmp_path / "missing-registry.yaml"),
            "execution_state_file": str(tmp_path / "execution-state.sqlite3"),
            "max_inflight": 2,
            "ack_wait_seconds": 10,
            "ack_progress_seconds": 1,
        },
    )
    adapter = BloodbankAdapter(config)
    adapter._js = FakeJetStream()
    adapter.ack_progress_seconds = 0.01
    return adapter


def write_registry(tmp_path, *, enabled=True, scope="fleet", target="bloodbank-pm"):
    registry = {
        "schema_version": 1,
        "agents": {
            "bloodbank-pm": {
                "profile_name": "bloodbank-pm",
                "bloodbank": {
                    "enabled": enabled,
                    "gateway_scope": scope,
                    "target_agent_id": target,
                },
            }
        },
    }
    (tmp_path / "missing-registry.yaml").write_text(
        json.dumps(registry), encoding="utf-8"
    )


def test_stale_durable_contract_is_rejected(tmp_path):
    adapter = make_adapter(tmp_path)
    stale = SimpleNamespace(
        filter_subject="bloodbank.cmd.v1.agent.invocation.other",
        ack_policy="explicit",
        max_ack_pending=adapter.max_inflight,
        max_deliver=adapter.max_deliver,
    )
    with pytest.raises(RuntimeError, match="existing Bloodbank durable"):
        adapter._assert_consumer_contract(stale)


def test_execution_state_is_private_and_refuses_symlink(tmp_path):
    make_adapter(tmp_path)
    state_path = tmp_path / "execution-state.sqlite3"
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

    target = tmp_path / "foreign-state.sqlite3"
    target.write_bytes(b"")
    link = tmp_path / "state-link.sqlite3"
    link.symlink_to(target)
    config = SimpleNamespace(
        enabled=True,
        extra={
            "target_profiles": {"bloodbank-pm": "bloodbank-pm"},
            "fleet_registry": str(tmp_path / "registry.yaml"),
            "execution_state_file": str(link),
        },
    )
    with pytest.raises(ValueError, match="must not be a symlink"):
        BloodbankAdapter(config)


@pytest.mark.asyncio
async def test_connect_binds_one_bounded_durable_pull_consumer(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path)
    calls = []
    never = asyncio.Event()

    class Subscription:
        async def fetch(self, *_args, **_kwargs):
            await never.wait()

    class JetStream:
        async def pull_subscribe(self, subject, **kwargs):
            calls.append((subject, kwargs))
            return Subscription()

        async def consumer_info(self, _stream, _consumer, timeout=None):
            del timeout
            return SimpleNamespace(config=calls[0][1]["config"])

    class Connection:
        def jetstream(self):
            return JetStream()

        async def drain(self):
            return None

    async def connect(**_kwargs):
        return Connection()

    class ConsumerConfig(SimpleNamespace):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    nats_module = types.ModuleType("nats")
    nats_module.__path__ = []
    nats_module.connect = connect
    nats_js = types.ModuleType("nats.js")
    nats_js.__path__ = []
    nats_api = types.ModuleType("nats.js.api")
    nats_api.AckPolicy = SimpleNamespace(EXPLICIT="explicit")
    nats_api.DeliverPolicy = SimpleNamespace(ALL="all")
    nats_api.ConsumerConfig = ConsumerConfig
    nats_errors = types.ModuleType("nats.errors")
    nats_errors.TimeoutError = TimeoutError
    monkeypatch.setitem(sys.modules, "nats", nats_module)
    monkeypatch.setitem(sys.modules, "nats.js", nats_js)
    monkeypatch.setitem(sys.modules, "nats.js.api", nats_api)
    monkeypatch.setitem(sys.modules, "nats.errors", nats_errors)
    assert await adapter.connect() is True
    assert len(calls) == 1
    subject, kwargs = calls[0]
    assert subject == "bloodbank.cmd.v1.agent.invocation.start"
    assert kwargs["durable"] == "bloodbank-hermes-gateway-v1"
    assert kwargs["stream"] == "BLOODBANK_COMMANDS"
    assert kwargs["config"].max_ack_pending == 2
    assert kwargs["pending_msgs_limit"] == 2
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_consume_loop_treats_asyncio_timeout_as_idle_poll(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path)
    adapter._running = True
    polls = 0

    class Subscription:
        async def fetch(self, *_args, **_kwargs):
            nonlocal polls
            polls += 1
            if polls == 1:
                raise TimeoutError
            adapter._running = False
            return []

    nats_errors = types.ModuleType("nats.errors")
    nats_errors.TimeoutError = type("NatsTimeoutError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "nats.errors", nats_errors)
    adapter._subscription = Subscription()

    await asyncio.wait_for(adapter._consume_loop(), timeout=1)

    assert polls == 2


@pytest.mark.asyncio
async def test_close_nats_bounds_drain_and_falls_back_to_close(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path)
    adapter.publish_timeout_seconds = 0.01
    closed = asyncio.Event()

    class Connection:
        async def drain(self):
            await asyncio.Event().wait()

        async def close(self):
            closed.set()

    adapter._nc = Connection()

    await asyncio.wait_for(adapter._close_nats(), timeout=1)

    assert closed.is_set()
    assert adapter._nc is None


@pytest.mark.asyncio
async def test_ack_waits_for_hermes_and_sends_progress(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(event):
        assert event.internal is True
        assert event.source.profile == "bloodbank-pm"
        assert event.raw_message["command_id"] == valid_command["command_id"]
        started.set()
        await release.wait()
        return "done"

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    task = asyncio.create_task(adapter._handle_broker_message(message))

    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0.03)
    assert message.acked == 0
    assert message.progress >= 1

    release.set()
    await asyncio.wait_for(task, timeout=1)
    assert message.acked == 1
    assert message.nacked == 0
    assert [event["type"] for event in adapter._js.events] == [
        "bloodbank.v1.conversation.turn.started",
        "bloodbank.v1.agent.invocation.started",
        "bloodbank.v1.agent.invocation.completed",
        "bloodbank.v1.conversation.turn.completed",
    ]


@pytest.mark.asyncio
async def test_failed_hermes_turn_publishes_failure_then_acks(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)

    async def handler(_event):
        raise RuntimeError("secret details must not enter the event")

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)

    assert message.acked == 1
    assert message.nacked == 0
    assert [event["type"] for event in adapter._js.events[-2:]] == [
        "bloodbank.v1.agent.invocation.failed",
        "bloodbank.v1.conversation.turn.completed",
    ]
    assert "secret details" not in json.dumps(adapter._js.events)


@pytest.mark.asyncio
async def test_invalid_and_unknown_targets_are_terminal(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    adapter.set_message_handler(lambda _event: None)

    malformed = dict(valid_command)
    malformed["kind"] = "event"
    malformed_message = FakeMessage(malformed)
    await adapter._handle_broker_message(malformed_message)
    assert malformed_message.termed == 1
    assert malformed_message.acked == 0

    (tmp_path / "missing-registry.yaml").write_text(
        "schema_version: 1\nagents: {}\n", encoding="utf-8"
    )
    unknown = json.loads(json.dumps(valid_command))
    unknown["command_id"] = "f482fce5-8d0f-43cc-9f93-ab94624637a8"
    unknown["data"]["target_agent_id"] = "unknown-agent"
    unknown_message = FakeMessage(unknown)
    await adapter._handle_broker_message(unknown_message)
    assert unknown_message.termed == 1
    assert unknown_message.acked == 0


@pytest.mark.asyncio
async def test_registry_outage_is_retryable_not_terminal(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    unavailable = json.loads(json.dumps(valid_command))
    unavailable["data"]["target_agent_id"] = "registry-only-agent"
    message = FakeMessage(unavailable)

    await adapter._handle_broker_message(message)

    assert message.nacked == 1
    assert message.termed == 0
    assert message.acked == 0


@pytest.mark.asyncio
async def test_active_registry_route_dispatches_and_completes(tmp_path, valid_command):
    write_registry(tmp_path)
    adapter = make_adapter(tmp_path, target_profiles={})
    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1
        return "done"

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)

    assert executions == 1
    assert message.acked == 1
    assert message.termed == 0
    record = adapter.execution_state.get(valid_command["command_id"])
    assert record is not None and record.state == "completed"


@pytest.mark.asyncio
async def test_policy_disabled_registry_route_is_terminal_before_claim(
    tmp_path, valid_command
):
    write_registry(tmp_path, enabled=False)
    adapter = make_adapter(tmp_path, target_profiles={})
    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)

    assert executions == 0
    assert message.termed == 1
    assert message.nacked == 0
    assert adapter.execution_state.get(valid_command["command_id"]) is None


@pytest.mark.asyncio
async def test_route_disabled_between_claim_and_dispatch_is_durably_rejected(
    tmp_path, valid_command
):
    write_registry(tmp_path)
    adapter = make_adapter(tmp_path, target_profiles={})
    original_claim = adapter.execution_state.claim_pending
    executions = 0

    def claim_then_disable(**kwargs):
        record = original_claim(**kwargs)
        write_registry(tmp_path, enabled=False)
        return record

    adapter.execution_state.claim_pending = claim_then_disable

    async def handler(_event):
        nonlocal executions
        executions += 1

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)

    assert executions == 0
    assert message.termed == 1
    assert message.nacked == 0
    assert adapter._js.events == []
    rejected = adapter.execution_state.get(valid_command["command_id"])
    assert rejected is not None
    assert rejected.state == "rejected"
    assert rejected.rejection_reason == ROUTE_REJECTION_REASON


@pytest.mark.asyncio
async def test_route_disabled_during_pre_dispatch_publish_cannot_execute(
    tmp_path, valid_command
):
    write_registry(tmp_path)
    adapter = make_adapter(tmp_path, target_profiles={})
    original_publish_many = adapter._publish_many
    publish_calls = 0
    executions = 0

    async def publish_then_disable(events):
        nonlocal publish_calls
        await original_publish_many(events)
        publish_calls += 1
        if publish_calls == 1:
            write_registry(tmp_path, enabled=False)

    async def handler(_event):
        nonlocal executions
        executions += 1

    adapter._publish_many = publish_then_disable
    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)

    assert executions == 0
    assert message.termed == 1
    assert [event["type"] for event in adapter._js.events] == [
        "bloodbank.v1.conversation.turn.started",
        "bloodbank.v1.agent.invocation.started",
    ]
    rejected = adapter.execution_state.get(valid_command["command_id"])
    assert rejected is not None and rejected.state == "rejected"


@pytest.mark.asyncio
async def test_pending_restart_rechecks_policy_and_rejected_redelivery_cannot_bypass(
    tmp_path, valid_command
):
    write_registry(tmp_path)
    first = make_adapter(tmp_path, target_profiles={})
    profile = first.profile_resolver.resolve(valid_command["data"]["target_agent_id"])
    invocation = Invocation.from_envelope(valid_command, profile)
    first.execution_state.claim_pending(
        command_id=valid_command["command_id"],
        digest=envelope_digest(valid_command),
        profile=profile,
        started_events=started_events(invocation),
    )
    write_registry(tmp_path, enabled=False)

    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1

    restarted = make_adapter(tmp_path, target_profiles={})
    restarted.set_message_handler(handler)
    disabled_redelivery = FakeMessage(valid_command)
    await restarted._handle_broker_message(disabled_redelivery)

    assert executions == 0
    assert disabled_redelivery.termed == 1
    rejected = restarted.execution_state.get(valid_command["command_id"])
    assert rejected is not None and rejected.state == "rejected"

    write_registry(tmp_path, enabled=True)
    reenabled = make_adapter(tmp_path, target_profiles={})
    reenabled.set_message_handler(handler)
    later_redelivery = FakeMessage(valid_command)
    await reenabled._handle_broker_message(later_redelivery)

    assert executions == 0
    assert later_redelivery.termed == 1
    assert later_redelivery.acked == 0


@pytest.mark.asyncio
async def test_pending_restart_with_unreadable_registry_remains_retryable(
    tmp_path, valid_command
):
    write_registry(tmp_path)
    first = make_adapter(tmp_path, target_profiles={})
    profile = first.profile_resolver.resolve(valid_command["data"]["target_agent_id"])
    invocation = Invocation.from_envelope(valid_command, profile)
    first.execution_state.claim_pending(
        command_id=valid_command["command_id"],
        digest=envelope_digest(valid_command),
        profile=profile,
        started_events=started_events(invocation),
    )
    (tmp_path / "missing-registry.yaml").write_text(
        "agents: [invalid\n", encoding="utf-8"
    )

    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1
        return "done"

    unavailable = make_adapter(tmp_path, target_profiles={})
    unavailable.set_message_handler(handler)
    transient_redelivery = FakeMessage(valid_command)
    await unavailable._handle_broker_message(transient_redelivery)

    assert executions == 0
    assert transient_redelivery.nacked == 1
    pending = unavailable.execution_state.get(valid_command["command_id"])
    assert pending is not None and pending.state == "pending"

    write_registry(tmp_path)
    recovered = make_adapter(tmp_path, target_profiles={})
    recovered.set_message_handler(handler)
    recovered_redelivery = FakeMessage(valid_command)
    await recovered._handle_broker_message(recovered_redelivery)

    assert executions == 1
    assert recovered_redelivery.acked == 1


@pytest.mark.asyncio
async def test_explicit_static_route_intentionally_bypasses_registry_policy(
    tmp_path, valid_command
):
    write_registry(tmp_path, enabled=False)
    adapter = make_adapter(tmp_path)
    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1
        return "done"

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)

    assert executions == 1
    assert message.acked == 1


def test_execution_journal_migrates_pending_rows_to_rejection_capable_schema(tmp_path):
    path = tmp_path / "legacy-state.sqlite3"
    command_id = "e7e00a9e-d38e-47df-8c01-917a6243e6af"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE executions (
                command_id TEXT PRIMARY KEY,
                envelope_digest TEXT NOT NULL,
                profile TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'completed')),
                outcome TEXT CHECK (outcome IN ('success', 'failure', 'cancelled')),
                started_events TEXT NOT NULL,
                terminal_events TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (
                    (state = 'pending' AND outcome IS NULL AND terminal_events IS NULL)
                    OR
                    (state = 'completed' AND outcome IS NOT NULL AND terminal_events IS NOT NULL)
                )
            );
            """
        )
        db.execute(
            """INSERT INTO executions VALUES (?, ?, ?, 'pending', NULL, ?, NULL, ?, ?)""",
            (command_id, "legacy-digest", "bloodbank-pm", "[]", "then", "then"),
        )

    store = ExecutionStateStore(path)
    migrated = store.get(command_id)
    assert migrated is not None and migrated.state == "pending"
    rejected = store.mark_rejected(
        command_id=command_id,
        digest="legacy-digest",
        reason=ROUTE_REJECTION_REASON,
    )

    assert rejected.state == "rejected"
    assert rejected.rejection_reason == ROUTE_REJECTION_REASON
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_terminal_publish_failure_naks_instead_of_acking(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    publish_count = 0

    async def publish(subject, payload, **kwargs):
        nonlocal publish_count
        del subject, payload, kwargs
        publish_count += 1
        if publish_count >= 3:
            raise RuntimeError("broker unavailable")

    adapter._js.publish = publish

    async def handler(_event):
        return "done"

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    assert message.acked == 0
    assert message.nacked == 1


@pytest.mark.asyncio
async def test_completed_command_replays_after_terminal_publish_failure_without_execution(
    tmp_path, valid_command
):
    first = make_adapter(tmp_path)
    executions = 0
    publish_count = 0

    async def failing_publish(subject, payload, **kwargs):
        nonlocal publish_count
        del subject, payload, kwargs
        publish_count += 1
        if publish_count >= 3:
            raise RuntimeError("broker unavailable after Hermes completed")

    first._js.publish = failing_publish

    async def handler(_event):
        nonlocal executions
        executions += 1
        return "done"

    first.set_message_handler(handler)
    original = FakeMessage(valid_command)
    await first._handle_broker_message(original)

    assert executions == 1
    assert original.nacked == 1
    completed = first.execution_state.get(valid_command["command_id"])
    assert completed is not None
    assert completed.state == "completed"
    assert completed.outcome == "success"

    restarted = make_adapter(tmp_path)

    async def must_not_execute(_event):
        raise AssertionError("completed command executed twice")

    restarted.set_message_handler(must_not_execute)
    redelivery = FakeMessage(valid_command)
    await restarted._handle_broker_message(redelivery)

    assert executions == 1
    assert redelivery.acked == 1
    assert redelivery.nacked == 0
    assert tuple(restarted._js.events) == (
        *completed.started_events,
        *completed.terminal_events,
    )
    assert [event["type"] for event in restarted._js.events] == [
        "bloodbank.v1.conversation.turn.started",
        "bloodbank.v1.agent.invocation.started",
        "bloodbank.v1.agent.invocation.completed",
        "bloodbank.v1.conversation.turn.completed",
    ]


@pytest.mark.asyncio
async def test_completed_command_replays_after_ack_failure_without_execution(
    tmp_path, valid_command
):
    first = make_adapter(tmp_path)
    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1
        return "done"

    first.set_message_handler(handler)
    original = FakeMessage(valid_command, fail_ack=True)
    await first._handle_broker_message(original)

    assert executions == 1
    assert original.nacked == 1
    completed = first.execution_state.get(valid_command["command_id"])
    assert completed is not None and completed.state == "completed"

    restarted = make_adapter(tmp_path)
    restarted.set_message_handler(handler)
    redelivery = FakeMessage(valid_command)
    await restarted._handle_broker_message(redelivery)

    assert executions == 1
    assert redelivery.acked == 1


@pytest.mark.asyncio
async def test_concurrent_redelivery_never_dispatches_second_hermes_turn(
    tmp_path, valid_command
):
    adapter = make_adapter(tmp_path)
    executions = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_event):
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        return "done"

    adapter.set_message_handler(handler)
    first = FakeMessage(valid_command)
    duplicate = FakeMessage(valid_command)
    first_task = asyncio.create_task(adapter._handle_broker_message(first))
    await asyncio.wait_for(started.wait(), timeout=1)
    await adapter._handle_broker_message(duplicate)

    assert executions == 1
    assert duplicate.nacked == 1
    assert duplicate.acked == 0

    release.set()
    await asyncio.wait_for(first_task, timeout=1)
    assert first.acked == 1


@pytest.mark.asyncio
async def test_command_id_collision_is_terminal_and_never_reexecutes(
    tmp_path, valid_command
):
    adapter = make_adapter(tmp_path)
    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1
        return "done"

    adapter.set_message_handler(handler)
    first = FakeMessage(valid_command)
    await adapter._handle_broker_message(first)
    assert executions == 1

    collision = json.loads(json.dumps(valid_command))
    collision["data"]["prompt"] = "Different command with a reused command_id."
    second = FakeMessage(collision)
    await adapter._handle_broker_message(second)

    assert executions == 1
    assert second.termed == 1
    assert second.acked == 0
