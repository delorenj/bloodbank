from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import stat
import sys
import types
import uuid
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


def make_adapter(tmp_path, *, target_profiles=None, allow_direct=False):
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
            "allow_direct_profile_targets": allow_direct,
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
        filter_subject="bloodbank.cmd.agent.invocation.other",
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
    assert subject == "bloodbank.cmd.agent.invocation.start"
    assert kwargs["durable"] == "bloodbank-hermes-gateway"
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
        "bloodbank.conversation.turn.started",
        "bloodbank.agent.invocation.started",
        "bloodbank.agent.invocation.completed",
        "bloodbank.conversation.turn.completed",
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
        "bloodbank.agent.invocation.failed",
        "bloodbank.conversation.turn.completed",
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


@pytest.mark.parametrize(
    "registry_text",
    [
        "",
        "schema_version: 1\n",
        "schema_version: 2\nagents: {}\n",
        "schema_version: 1\nagents:\n  123: {}\n",
        "schema_version: 1\nagents:\n  bloodbank-pm: []\n",
        (
            "schema_version: 1\nagents:\n"
            "  ' bloodbank-pm ':\n"
            "    profile_name: bloodbank-pm\n"
            "    bloodbank:\n"
            "      enabled: false\n"
            "      gateway_scope: fleet\n"
            "      target_agent_id: ' bloodbank-pm '\n"
        ),
        (
            "schema_version: 1\nagents:\n"
            "  Bloodbank-pm:\n"
            "    profile_name: bloodbank-pm\n"
            "    bloodbank:\n"
            "      enabled: false\n"
            "      gateway_scope: fleet\n"
            "      target_agent_id: Bloodbank-pm\n"
        ),
    ],
    ids=(
        "empty-file",
        "missing-agents",
        "unsupported-version",
        "non-string-agent-key",
        "malformed-agent-metadata",
        "padded-disabled-agent-key",
        "case-variant-disabled-agent-key",
    ),
)
@pytest.mark.asyncio
async def test_invalid_registry_is_transient_and_cannot_use_direct_fallback(
    tmp_path, valid_command, registry_text
):
    (tmp_path / "missing-registry.yaml").write_text(registry_text, encoding="utf-8")
    adapter = make_adapter(tmp_path, target_profiles={}, allow_direct=True)
    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)

    assert executions == 0
    assert message.nacked == 1
    assert message.termed == 0
    assert message.acked == 0
    assert adapter.execution_state.get(valid_command["command_id"]) is None


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
    assert rejected.state == "rejected_pre_start"
    assert rejected.rejection_reason == ROUTE_REJECTION_REASON
    assert rejected.terminal_events == ()


@pytest.mark.asyncio
async def test_route_disabled_after_started_publish_closes_and_replays_exactly(
    tmp_path, valid_command, repo_root
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
        "bloodbank.conversation.turn.started",
        "bloodbank.agent.invocation.started",
        "bloodbank.agent.invocation.failed",
        "bloodbank.conversation.turn.completed",
    ]
    rejected = adapter.execution_state.get(valid_command["command_id"])
    assert rejected is not None and rejected.state == "rejected_closed"
    assert tuple(adapter._js.events[-2:]) == rejected.terminal_events
    assert rejected.terminal_events[0]["data"]["error_code"] == ROUTE_REJECTION_REASON
    assert (
        rejected.terminal_events[0]["causationid"] == rejected.started_events[1]["id"]
    )
    assert (
        rejected.terminal_events[1]["causationid"] == rejected.terminal_events[0]["id"]
    )
    sys.path.insert(0, str(repo_root / "services" / "agent-hooks"))
    from core.validate import validate_envelope

    for event in rejected.terminal_events:
        validate_envelope(event)

    write_registry(tmp_path, enabled=True)
    restarted = make_adapter(tmp_path, target_profiles={})

    async def must_not_execute(_event):
        raise AssertionError("a rejected command executed after restart")

    restarted.set_message_handler(must_not_execute)
    exact_redelivery = FakeMessage(valid_command)
    await restarted._handle_broker_message(exact_redelivery)

    assert exact_redelivery.termed == 1
    assert exact_redelivery.nacked == 0
    assert tuple(restarted._js.events) == (
        *rejected.started_events,
        *rejected.terminal_events,
    )


@pytest.mark.asyncio
async def test_partial_started_publish_is_closed_on_disabled_redelivery(
    tmp_path, valid_command
):
    write_registry(tmp_path)
    first = make_adapter(tmp_path, target_profiles={})
    published = []
    publish_count = 0
    executions = 0

    async def fail_second_start(subject, payload, **kwargs):
        nonlocal publish_count
        del subject, kwargs
        publish_count += 1
        if publish_count == 2:
            raise RuntimeError("second started event was not acknowledged")
        published.append(json.loads(payload))

    async def handler(_event):
        nonlocal executions
        executions += 1

    first._js.publish = fail_second_start
    first.set_message_handler(handler)
    first_delivery = FakeMessage(valid_command)
    await first._handle_broker_message(first_delivery)

    assert executions == 0
    assert first_delivery.nacked == 1
    assert first_delivery.termed == 0
    assert [event["type"] for event in published] == [
        "bloodbank.conversation.turn.started"
    ]
    opened = first.execution_state.get(valid_command["command_id"])
    assert opened is not None and opened.state == "started"

    write_registry(tmp_path, enabled=False)
    restarted = make_adapter(tmp_path, target_profiles={})
    restarted.set_message_handler(handler)
    redelivery = FakeMessage(valid_command)
    await restarted._handle_broker_message(redelivery)

    assert executions == 0
    assert redelivery.termed == 1
    assert redelivery.nacked == 0
    assert [event["type"] for event in restarted._js.events] == [
        "bloodbank.conversation.turn.started",
        "bloodbank.agent.invocation.started",
        "bloodbank.agent.invocation.failed",
        "bloodbank.conversation.turn.completed",
    ]
    closed = restarted.execution_state.get(valid_command["command_id"])
    assert closed is not None and closed.state == "rejected_closed"


@pytest.mark.asyncio
async def test_rejection_persistence_failure_naks_then_restart_closes_without_execution(
    tmp_path, valid_command
):
    write_registry(tmp_path)
    first = make_adapter(tmp_path, target_profiles={})
    original_publish_many = first._publish_many
    original_finalize = first.execution_state.finalize_rejection
    publish_calls = 0
    finalize_calls = 0
    executions = 0

    async def publish_then_disable(events):
        nonlocal publish_calls
        await original_publish_many(events)
        publish_calls += 1
        if publish_calls == 1:
            write_registry(tmp_path, enabled=False)

    def fail_first_finalize(**kwargs):
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            raise RuntimeError("journal write unavailable")
        return original_finalize(**kwargs)

    async def handler(_event):
        nonlocal executions
        executions += 1

    first._publish_many = publish_then_disable
    first.execution_state.finalize_rejection = fail_first_finalize
    first.set_message_handler(handler)
    first_delivery = FakeMessage(valid_command)
    await first._handle_broker_message(first_delivery)

    assert executions == 0
    assert first_delivery.nacked == 1
    assert first_delivery.termed == 0
    closing = first.execution_state.get(valid_command["command_id"])
    assert closing is not None and closing.state == "rejected_closing"
    assert closing.terminal_events == ()

    write_registry(tmp_path, enabled=True)
    restarted = make_adapter(tmp_path, target_profiles={})
    restarted.set_message_handler(handler)
    redelivery = FakeMessage(valid_command)
    await restarted._handle_broker_message(redelivery)

    assert executions == 0
    assert redelivery.termed == 1
    assert redelivery.nacked == 0
    closed = restarted.execution_state.get(valid_command["command_id"])
    assert closed is not None and closed.state == "rejected_closed"
    assert {event["time"] for event in closed.terminal_events} == {closing.rejected_at}


@pytest.mark.asyncio
async def test_partial_terminal_publish_naks_and_exact_restart_replays_closure(
    tmp_path, valid_command
):
    write_registry(tmp_path)
    first = make_adapter(tmp_path, target_profiles={})
    original_publish_many = first._publish_many
    published = []
    publish_calls = 0
    executions = 0

    async def fail_turn_terminal(subject, payload, **kwargs):
        del subject, kwargs
        envelope = json.loads(payload)
        if envelope["type"] == "bloodbank.conversation.turn.completed":
            raise RuntimeError("terminal turn event was not acknowledged")
        published.append(envelope)

    async def publish_then_disable(events):
        nonlocal publish_calls
        await original_publish_many(events)
        publish_calls += 1
        if publish_calls == 1:
            write_registry(tmp_path, enabled=False)

    async def handler(_event):
        nonlocal executions
        executions += 1

    first._js.publish = fail_turn_terminal
    first._publish_many = publish_then_disable
    first.set_message_handler(handler)
    first_delivery = FakeMessage(valid_command)
    await first._handle_broker_message(first_delivery)

    assert executions == 0
    assert first_delivery.nacked == 1
    assert first_delivery.termed == 0
    assert [event["type"] for event in published] == [
        "bloodbank.conversation.turn.started",
        "bloodbank.agent.invocation.started",
        "bloodbank.agent.invocation.failed",
    ]
    closed = first.execution_state.get(valid_command["command_id"])
    assert closed is not None and closed.state == "rejected_closed"

    write_registry(tmp_path, enabled=True)
    restarted = make_adapter(tmp_path, target_profiles={})
    restarted.set_message_handler(handler)
    redelivery = FakeMessage(valid_command)
    await restarted._handle_broker_message(redelivery)

    assert executions == 0
    assert redelivery.termed == 1
    assert redelivery.nacked == 0
    assert tuple(restarted._js.events) == (
        *closed.started_events,
        *closed.terminal_events,
    )


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
    assert rejected is not None and rejected.state == "rejected_pre_start"

    write_registry(tmp_path, enabled=True)
    reenabled = make_adapter(tmp_path, target_profiles={})
    reenabled.set_message_handler(handler)
    later_redelivery = FakeMessage(valid_command)
    await reenabled._handle_broker_message(later_redelivery)

    assert executions == 0
    assert later_redelivery.termed == 1
    assert later_redelivery.acked == 0
    assert later_redelivery.nacked == 0
    assert reenabled._js.events == []


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


def test_execution_journal_migrates_legacy_pending_as_possibly_started(tmp_path):
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
    assert migrated is not None and migrated.state == "started"
    rejected = store.begin_rejection(
        command_id=command_id,
        digest="legacy-digest",
        reason=ROUTE_REJECTION_REASON,
    )

    assert rejected.state == "rejected_closing"
    assert rejected.rejection_reason == ROUTE_REJECTION_REASON
    assert rejected.rejected_at is not None
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_v2_rejected_row_migrates_to_closure_without_execution(
    tmp_path, valid_command
):
    path = tmp_path / "execution-state.sqlite3"
    invocation = Invocation.from_envelope(valid_command, "bloodbank-pm")
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE executions (
                command_id TEXT PRIMARY KEY,
                envelope_digest TEXT NOT NULL,
                profile TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'completed', 'rejected')),
                outcome TEXT CHECK (outcome IN ('success', 'failure', 'cancelled')),
                started_events TEXT NOT NULL,
                terminal_events TEXT,
                rejection_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (
                    (state = 'pending' AND outcome IS NULL
                     AND terminal_events IS NULL AND rejection_reason IS NULL)
                    OR
                    (state = 'completed' AND outcome IS NOT NULL
                     AND terminal_events IS NOT NULL AND rejection_reason IS NULL)
                    OR
                    (state = 'rejected' AND outcome IS NULL
                     AND terminal_events IS NULL AND rejection_reason IS NOT NULL)
                )
            );
            PRAGMA user_version = 2;
            """
        )
        db.execute(
            """INSERT INTO executions VALUES (
                   ?, ?, ?, 'rejected', NULL, ?, NULL, ?, ?, ?
               )""",
            (
                valid_command["command_id"],
                envelope_digest(valid_command),
                "bloodbank-pm",
                json.dumps(started_events(invocation)),
                ROUTE_REJECTION_REASON,
                "2026-08-15T12:00:00Z",
                "2026-08-15T12:00:00Z",
            ),
        )

    write_registry(tmp_path, enabled=True)
    adapter = make_adapter(tmp_path, target_profiles={})
    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1

    adapter.set_message_handler(handler)
    redelivery = FakeMessage(valid_command)
    await adapter._handle_broker_message(redelivery)

    assert executions == 0
    assert redelivery.termed == 1
    assert redelivery.nacked == 0
    assert [event["type"] for event in adapter._js.events] == [
        "bloodbank.conversation.turn.started",
        "bloodbank.agent.invocation.started",
        "bloodbank.agent.invocation.failed",
        "bloodbank.conversation.turn.completed",
    ]
    migrated = adapter.execution_state.get(valid_command["command_id"])
    assert migrated is not None and migrated.state == "rejected_closed"


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
        "bloodbank.conversation.turn.started",
        "bloodbank.agent.invocation.started",
        "bloodbank.agent.invocation.completed",
        "bloodbank.conversation.turn.completed",
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


# --------------------------------------------------------------------------
# Interactive lane (bloodbank.agent.invocation.invoke)
# --------------------------------------------------------------------------

INTERACTIVE_EXTRA = {
    "enabled": True,
    "durable_name": "bloodbank-hermes-gateway-interactive",
    "max_inflight": 2,
    "max_deliver": 3,
    "ack_wait_seconds": 180,
    "ack_progress_seconds": 30,
    "nak_delay_seconds": 2,
}


def make_lane_adapter(tmp_path, *, interactive=True, max_inflight=2):
    adapter = make_adapter(tmp_path)
    extra = adapter.config.extra
    extra["max_inflight"] = max_inflight
    if interactive is not None:
        extra["interactive"] = dict(INTERACTIVE_EXTRA) if interactive else {
            "enabled": False
        }
    adapter = BloodbankAdapter(adapter.config)
    adapter._js = FakeJetStream()
    for lane in adapter.lanes:
        lane.ack_progress = 0.01
    return adapter


def as_interactive(command):
    """The interactive-lane twin of a batch command fixture."""

    twin = json.loads(json.dumps(command))
    twin["type"] = "bloodbank.agent.invocation.invoke"
    twin["dataschema"] = "apicurio://holyfields/bloodbank.agent.invocation.invoke/versions/1"
    twin["schemaref"] = "bloodbank.agent.invocation.invoke.v1"
    return twin


def lane(adapter, name):
    return next(item for item in adapter.lanes if item.name == name)


def test_interactive_lane_is_off_unless_explicitly_enabled(tmp_path):
    assert [item.name for item in make_adapter(tmp_path).lanes] == ["batch"]
    assert [item.name for item in make_lane_adapter(tmp_path, interactive=None).lanes] == [
        "batch"
    ]
    assert [item.name for item in make_lane_adapter(tmp_path, interactive=False).lanes] == [
        "batch"
    ]
    adapter = make_lane_adapter(tmp_path)
    assert [item.name for item in adapter.lanes] == ["batch", "interactive"]
    interactive = lane(adapter, "interactive")
    assert interactive.durable == "bloodbank-hermes-gateway-interactive"
    assert (interactive.max_inflight, interactive.max_deliver) == (2, 3)
    assert interactive.spec.subject == "bloodbank.cmd.agent.invocation.invoke"
    # The batch lane keeps its own, unrelated bounds.
    assert lane(adapter, "batch").max_inflight == 2


def test_interactive_lane_configuration_is_strict(tmp_path):
    base = make_adapter(tmp_path)
    for bad in (
        {"enabled": "true"},
        {"enabled": True, "max_inflight": 0},
        {"enabled": True, "ack_wait_seconds": 30, "ack_progress_seconds": 30},
        {"enabled": True, "durable_name": "bloodbank-hermes-gateway"},
        {"enabled": True, "durable_name": "  "},
    ):
        base.config.extra["interactive"] = bad
        with pytest.raises(ValueError):
            BloodbankAdapter(base.config)
    base.config.extra["interactive"] = "yes"
    with pytest.raises(ValueError, match="mapping"):
        BloodbankAdapter(base.config)


def test_stale_interactive_durable_contract_is_rejected(tmp_path):
    adapter = make_lane_adapter(tmp_path)
    interactive = lane(adapter, "interactive")
    good = SimpleNamespace(
        filter_subject=interactive.spec.subject,
        ack_policy="explicit",
        max_ack_pending=2,
        max_deliver=3,
    )
    adapter._assert_consumer_contract(good, interactive)
    # The batch durable's filter is not acceptable for the interactive lane.
    wrong_filter = SimpleNamespace(**{**vars(good), "filter_subject": lane(adapter, "batch").spec.subject})
    with pytest.raises(RuntimeError, match="existing Bloodbank durable"):
        adapter._assert_consumer_contract(wrong_filter, interactive)
    wrong_slots = SimpleNamespace(**{**vars(good), "max_ack_pending": 4})
    with pytest.raises(RuntimeError, match="existing Bloodbank durable"):
        adapter._assert_consumer_contract(wrong_slots, interactive)


@pytest.mark.asyncio
async def test_connect_binds_a_second_bounded_durable_per_lane(tmp_path, monkeypatch):
    adapter = make_lane_adapter(tmp_path)
    calls = []
    never = asyncio.Event()

    class Subscription:
        async def fetch(self, *_args, **_kwargs):
            await never.wait()

    class JetStream:
        async def pull_subscribe(self, subject, **kwargs):
            calls.append((subject, kwargs))
            return Subscription()

        async def consumer_info(self, _stream, durable, timeout=None):
            del timeout
            config = next(
                kwargs["config"] for _s, kwargs in calls if kwargs["durable"] == durable
            )
            return SimpleNamespace(config=config)

    class Connection:
        def jetstream(self):
            return JetStream()

        async def drain(self):
            return None

    async def connect(**_kwargs):
        return Connection()

    nats_module = types.ModuleType("nats")
    nats_module.__path__ = []
    nats_module.connect = connect
    nats_js = types.ModuleType("nats.js")
    nats_js.__path__ = []
    nats_api = types.ModuleType("nats.js.api")
    nats_api.AckPolicy = SimpleNamespace(EXPLICIT="explicit")
    nats_api.DeliverPolicy = SimpleNamespace(ALL="all")
    nats_api.ConsumerConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    nats_errors = types.ModuleType("nats.errors")
    nats_errors.TimeoutError = TimeoutError
    monkeypatch.setitem(sys.modules, "nats", nats_module)
    monkeypatch.setitem(sys.modules, "nats.js", nats_js)
    monkeypatch.setitem(sys.modules, "nats.js.api", nats_api)
    monkeypatch.setitem(sys.modules, "nats.errors", nats_errors)

    assert await adapter.connect() is True
    by_subject = {subject: kwargs for subject, kwargs in calls}
    assert set(by_subject) == {
        "bloodbank.cmd.agent.invocation.start",
        "bloodbank.cmd.agent.invocation.invoke",
    }
    batch = by_subject["bloodbank.cmd.agent.invocation.start"]
    interactive = by_subject["bloodbank.cmd.agent.invocation.invoke"]
    assert batch["durable"] == "bloodbank-hermes-gateway"
    assert interactive["durable"] == "bloodbank-hermes-gateway-interactive"
    assert interactive["stream"] == batch["stream"] == "BLOODBANK_COMMANDS"
    assert interactive["config"].max_ack_pending == 2
    assert interactive["config"].max_deliver == 3
    assert interactive["config"].filter_subject == "bloodbank.cmd.agent.invocation.invoke"
    assert interactive["config"].ack_wait == 180
    assert interactive["pending_msgs_limit"] == 2
    assert all(item.task is not None for item in adapter.lanes)
    await adapter.disconnect()
    assert all(item.task is None and item.subscription is None for item in adapter.lanes)


@pytest.mark.asyncio
async def test_one_lane_failing_to_bind_fails_the_whole_connect(tmp_path, monkeypatch):
    adapter = make_lane_adapter(tmp_path)

    class JetStream:
        async def pull_subscribe(self, subject, **kwargs):
            if subject.endswith(".invoke"):
                raise RuntimeError("filter overlaps another consumer")
            return SimpleNamespace()

        async def consumer_info(self, _stream, _durable, timeout=None):
            del timeout
            return SimpleNamespace(
                config=SimpleNamespace(
                    filter_subject="bloodbank.cmd.agent.invocation.start",
                    ack_policy="explicit",
                    max_ack_pending=2,
                    max_deliver=5,
                )
            )

    class Connection:
        def jetstream(self):
            return JetStream()

        async def drain(self):
            return None

    async def connect(**_kwargs):
        return Connection()

    nats_module = types.ModuleType("nats")
    nats_module.__path__ = []
    nats_module.connect = connect
    nats_js = types.ModuleType("nats.js")
    nats_js.__path__ = []
    nats_api = types.ModuleType("nats.js.api")
    nats_api.AckPolicy = SimpleNamespace(EXPLICIT="explicit")
    nats_api.DeliverPolicy = SimpleNamespace(ALL="all")
    nats_api.ConsumerConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "nats", nats_module)
    monkeypatch.setitem(sys.modules, "nats.js", nats_js)
    monkeypatch.setitem(sys.modules, "nats.js.api", nats_api)

    assert await adapter.connect() is False
    assert all(item.task is None for item in adapter.lanes)
    assert adapter._nc is None


class QueueSubscription:
    """A pull subscription fake: fetch() returns what is queued, else times out."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.fetches = 0

    async def fetch(self, count, timeout=1.0):
        self.fetches += 1
        try:
            first = await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError from None
        batch = [first]
        while len(batch) < count and not self.queue.empty():
            batch.append(self.queue.get_nowait())
        return batch


@pytest.mark.asyncio
async def test_saturated_batch_lane_never_blocks_an_interactive_command(
    tmp_path, valid_command
):
    adapter = make_lane_adapter(tmp_path, max_inflight=2)
    batch, interactive = lane(adapter, "batch"), lane(adapter, "interactive")
    batch.subscription, interactive.subscription = QueueSubscription(), QueueSubscription()
    adapter.fetch_timeout_seconds = 0.05
    release_batch = asyncio.Event()
    ran = []

    async def handler(event):
        ran.append((event.source.chat_id, event.raw_message["command_id"]))
        if event.source.chat_id.startswith("groom-"):
            await release_batch.wait()
        return "done"

    adapter.set_message_handler(handler)

    batch_messages = []
    for index in range(3):
        command = json.loads(json.dumps(valid_command))
        command["command_id"] = str(uuid.uuid4())
        command["data"]["thread_id"] = f"groom-{index}"
        message = FakeMessage(command)
        batch_messages.append(message)
        batch.subscription.queue.put_nowait(message)

    adapter._running = True
    loops = [
        asyncio.create_task(adapter._consume_loop(batch)),
        asyncio.create_task(adapter._consume_loop(interactive)),
    ]
    try:
        # Two batch turns occupy both batch slots; the third is never fetched.
        for _ in range(100):
            if len(batch.inflight) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(batch.inflight) == 2
        assert batch.subscription.queue.qsize() == 1

        memo = as_interactive(valid_command)
        memo["command_id"] = str(uuid.uuid4())
        memo["data"]["thread_id"] = "memo-1"
        memo_message = FakeMessage(memo)
        interactive.subscription.queue.put_nowait(memo_message)
        for _ in range(100):
            if memo_message.acked:
                break
            await asyncio.sleep(0.01)

        assert memo_message.acked == 1
        assert not any(message.acked for message in batch_messages)
        assert ("memo-1", memo["command_id"]) in ran
        assert len(batch.inflight) == 2  # still saturated

        lifecycle = [event for event in adapter._js.events if event["data"]["command_id"] == memo["command_id"]]
        assert {event["data"]["lane"] for event in lifecycle} == {"interactive"}
    finally:
        release_batch.set()
        adapter._running = False
        for task in loops:
            task.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        await asyncio.gather(*(batch.inflight | interactive.inflight), return_exceptions=True)


@pytest.mark.asyncio
async def test_interactive_command_uses_the_same_journal_and_lifecycle(
    tmp_path, valid_command
):
    adapter = make_lane_adapter(tmp_path)
    interactive = lane(adapter, "interactive")
    handled = []

    async def handler(event):
        handled.append(event.raw_message["command_id"])
        return "done"

    adapter.set_message_handler(handler)
    memo = as_interactive(valid_command)
    message = FakeMessage(memo)
    await adapter._handle_broker_message(message, interactive)

    assert message.acked == 1 and message.termed == 0 and message.nacked == 0
    assert [event["type"] for event in adapter._js.events] == [
        "bloodbank.conversation.turn.started",
        "bloodbank.agent.invocation.started",
        "bloodbank.agent.invocation.completed",
        "bloodbank.conversation.turn.completed",
    ]
    assert {event["data"]["lane"] for event in adapter._js.events} == {"interactive"}
    # The lifecycle fact types and subjects are the batch lane's, unchanged.
    assert adapter._js.events[1]["subject"] == "bloodbank.evt.agent.invocation.started"
    record = adapter.execution_state.get(memo["command_id"])
    assert record is not None and record.state == "completed"
    assert record.lane == "interactive"


@pytest.mark.asyncio
async def test_batch_lane_stamps_its_lane_on_the_journal_and_lifecycle(
    tmp_path, valid_command
):
    adapter = make_lane_adapter(tmp_path)

    async def handler(_event):
        return "done"

    adapter.set_message_handler(handler)
    await adapter._handle_broker_message(FakeMessage(valid_command))
    record = adapter.execution_state.get(valid_command["command_id"])
    assert record is not None and record.lane == "batch"
    assert {event["data"]["lane"] for event in adapter._js.events} == {"batch"}


@pytest.mark.asyncio
async def test_a_command_on_the_wrong_lane_is_terminal(tmp_path, valid_command):
    adapter = make_lane_adapter(tmp_path)
    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1
        return "done"

    adapter.set_message_handler(handler)
    start_on_interactive = FakeMessage(valid_command)
    await adapter._handle_broker_message(start_on_interactive, lane(adapter, "interactive"))
    invoke_on_batch = FakeMessage(as_interactive(valid_command))
    await adapter._handle_broker_message(invoke_on_batch, lane(adapter, "batch"))

    assert executions == 0
    assert start_on_interactive.termed == 1 and start_on_interactive.acked == 0
    assert invoke_on_batch.termed == 1 and invoke_on_batch.acked == 0
    assert adapter._js.events == []


@pytest.mark.asyncio
async def test_a_command_id_can_never_run_twice_across_lanes(tmp_path, valid_command):
    adapter = make_lane_adapter(tmp_path)
    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1
        return "done"

    adapter.set_message_handler(handler)
    first = FakeMessage(valid_command)
    await adapter._handle_broker_message(first, lane(adapter, "batch"))
    twin = FakeMessage(as_interactive(valid_command))  # same command_id, other type
    await adapter._handle_broker_message(twin, lane(adapter, "interactive"))

    assert executions == 1
    assert first.acked == 1
    assert twin.termed == 1 and twin.acked == 0


@pytest.mark.asyncio
async def test_interactive_redelivery_replays_without_reexecuting(
    tmp_path, valid_command
):
    adapter = make_lane_adapter(tmp_path)
    interactive = lane(adapter, "interactive")
    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1
        return "done"

    adapter.set_message_handler(handler)
    memo = as_interactive(valid_command)
    await adapter._handle_broker_message(FakeMessage(memo, fail_ack=True), interactive)
    assert executions == 1
    published = len(adapter._js.events)

    redelivered = FakeMessage(memo)
    await adapter._handle_broker_message(redelivered, interactive)
    assert executions == 1
    assert redelivered.acked == 1
    # The stored lifecycle envelopes are replayed, with stable ids.
    assert len(adapter._js.events) == 2 * published
    assert len({event["id"] for event in adapter._js.events}) == published


@pytest.mark.asyncio
async def test_interactive_concurrent_redelivery_is_nakked_with_the_lane_delay(
    tmp_path, valid_command
):
    adapter = make_lane_adapter(tmp_path)
    interactive = lane(adapter, "interactive")
    started, release = asyncio.Event(), asyncio.Event()
    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        return "done"

    adapter.set_message_handler(handler)
    memo = as_interactive(valid_command)
    delays = []

    class Duplicate(FakeMessage):
        async def nak(self, delay=None):
            delays.append(delay)
            await super().nak(delay=delay)

    first = FakeMessage(memo)
    task = asyncio.create_task(adapter._handle_broker_message(first, interactive))
    await asyncio.wait_for(started.wait(), timeout=1)
    duplicate = Duplicate(memo)
    await adapter._handle_broker_message(duplicate, interactive)
    assert executions == 1 and duplicate.nacked == 1
    assert delays == [2.0]  # interactive nak_delay, not the batch 5s
    release.set()
    await asyncio.wait_for(task, timeout=1)


def test_journal_gains_a_nullable_lane_column_without_losing_rows(tmp_path):
    path = tmp_path / "pre-lane.sqlite3"
    command_id = "e7e00a9e-d38e-47df-8c01-917a6243e6af"
    # A v3 journal exactly as the gateway wrote it before lanes existed.
    store = ExecutionStateStore(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE executions")
        db.execute(
            """
            CREATE TABLE executions (
                command_id TEXT PRIMARY KEY,
                envelope_digest TEXT NOT NULL,
                profile TEXT NOT NULL,
                state TEXT NOT NULL,
                outcome TEXT,
                started_events TEXT NOT NULL,
                terminal_events TEXT,
                rejection_reason TEXT,
                rejected_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute("PRAGMA user_version = 3")
        db.execute(
            "INSERT INTO executions VALUES (?, 'd', 'bloodbank-pm', 'completed',"
            " 'success', '[]', '[]', NULL, NULL, 'then', 'then')",
            (command_id,),
        )
    del store

    migrated = ExecutionStateStore(path)
    row = migrated.get(command_id)
    assert row is not None and row.state == "completed" and row.lane is None
    claimed = migrated.claim_pending(
        command_id="aaaaaaaa-d38e-47df-8c01-917a6243e6af",
        digest="x",
        profile="bloodbank-pm",
        started_events=(),
        lane="interactive",
    )
    assert claimed.lane == "interactive"
    # Reopening an already-migrated journal is a no-op.
    assert ExecutionStateStore(path).get(claimed.command_id).lane == "interactive"


@pytest.mark.asyncio
async def test_concurrent_cross_lane_twin_of_a_running_command_is_terminated(
    tmp_path, valid_command
):
    """A twin on the other lane can never become valid: term it, do not nak it into a dead letter."""

    adapter = make_lane_adapter(tmp_path)
    batch, interactive = lane(adapter, "batch"), lane(adapter, "interactive")
    started, release = asyncio.Event(), asyncio.Event()
    executions = 0

    async def handler(_event):
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        return "done"

    adapter.set_message_handler(handler)
    memo = as_interactive(valid_command)
    first = FakeMessage(memo)
    task = asyncio.create_task(adapter._handle_broker_message(first, interactive))
    await asyncio.wait_for(started.wait(), timeout=1)

    twin = FakeMessage(valid_command)  # same command_id on the batch subject, original still running
    await adapter._handle_broker_message(twin, batch)
    assert twin.termed == 1 and twin.nacked == 0 and twin.acked == 0
    assert executions == 1

    # a same-lane redelivery of the running command is still just retried later
    again = FakeMessage(memo)
    await adapter._handle_broker_message(again, interactive)
    assert again.nacked == 1 and again.termed == 0

    release.set()
    await asyncio.wait_for(task, timeout=1)
    assert first.acked == 1 and executions == 1


@pytest.mark.asyncio
async def test_a_lane_loop_that_hits_an_unexpected_error_resumes_pulling(
    tmp_path, valid_command, monkeypatch
):
    adapter = make_lane_adapter(tmp_path)
    interactive = lane(adapter, "interactive")
    interactive.subscription = QueueSubscription()
    adapter.fetch_timeout_seconds = 0.05
    ran = []

    async def handler(event):
        ran.append(event.raw_message["command_id"])
        return "done"

    adapter.set_message_handler(handler)
    real_once = adapter._consume_once
    calls = 0

    async def flaky(lane_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AttributeError("boom")
        await real_once(lane_)

    monkeypatch.setattr(adapter, "_consume_once", flaky)
    adapter._running = True
    loop = asyncio.create_task(adapter._consume_loop(interactive))
    try:
        message = FakeMessage(as_interactive(valid_command))
        interactive.subscription.queue.put_nowait(message)
        for _ in range(300):
            if ran:
                break
            await asyncio.sleep(0.02)
        assert ran, "the lane never resumed after an unexpected error"
        assert not loop.done()
    finally:
        adapter._running = False
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)


def test_interactive_lane_defaults_give_a_retry_horizon_beyond_a_registry_rewrite(tmp_path):
    adapter = make_adapter(tmp_path)
    adapter.config.extra["interactive"] = {"enabled": True}
    interactive = lane(BloodbankAdapter(adapter.config), "interactive")
    # max_deliver x nak_delay is how long a transient registry/journal error keeps being retried
    assert interactive.max_deliver * interactive.nak_delay >= 20


# --- per-turn wall clock on the interactive lane ---------------------------


def make_timeout_adapter(tmp_path, *, timeout=0.2, max_inflight=2):
    adapter = make_lane_adapter(tmp_path, max_inflight=max_inflight)
    lane(adapter, "interactive").turn_timeout = timeout
    return adapter


def memo_command(valid_command, thread, **data):
    command = as_interactive(valid_command)
    command["command_id"] = str(uuid.uuid4())
    command["data"]["thread_id"] = thread
    command["data"].update(data)
    return command


def lifecycle_types(adapter, command):
    return [
        event["type"]
        for event in adapter._js.events
        if event["data"]["command_id"] == command["command_id"]
    ]


def failed_fact(adapter, command):
    return next(
        event
        for event in adapter._js.events
        if event["data"]["command_id"] == command["command_id"]
        and event["type"] == "bloodbank.agent.invocation.failed"
    )


def test_turn_timeout_defaults_and_validation(tmp_path):
    adapter = make_adapter(tmp_path)
    # absent on a lane that exists = the interactive default; batch has none
    adapter.config.extra["interactive"] = {"enabled": True}
    built = BloodbankAdapter(adapter.config)
    assert lane(built, "interactive").turn_timeout == 300.0
    assert lane(built, "batch").turn_timeout is None

    adapter.config.extra["interactive"] = {"enabled": True, "turn_timeout_seconds": 45}
    assert lane(BloodbankAdapter(adapter.config), "interactive").turn_timeout == 45.0
    adapter.config.extra["interactive"] = {"enabled": True, "turn_timeout_seconds": 0.5}
    assert lane(BloodbankAdapter(adapter.config), "interactive").turn_timeout == 0.5

    for bad in (0, -1, True, False, None, "300", float("nan"), float("inf"), 86_401, [], {}):
        adapter.config.extra["interactive"] = {
            "enabled": True,
            "turn_timeout_seconds": bad,
        }
        with pytest.raises(ValueError, match="turn_timeout_seconds"):
            BloodbankAdapter(adapter.config)


def test_batch_turn_timeout_is_opt_in_and_strict(tmp_path):
    adapter = make_adapter(tmp_path)
    adapter.config.extra["turn_timeout_seconds"] = 900
    assert lane(BloodbankAdapter(adapter.config), "batch").turn_timeout == 900.0
    adapter.config.extra["turn_timeout_seconds"] = 0
    with pytest.raises(ValueError, match="turn_timeout_seconds"):
        BloodbankAdapter(adapter.config)


@pytest.mark.asyncio
async def test_timeout_stops_the_turn_publishes_failed_and_acks_once(
    tmp_path, valid_command
):
    adapter = make_timeout_adapter(tmp_path, timeout=0.2)
    interactive = lane(adapter, "interactive")
    cancelled = asyncio.Event()

    async def handler(_event):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-slow")
    message = FakeMessage(memo)
    await asyncio.wait_for(adapter._handle_broker_message(message, interactive), 5)

    # the Hermes turn was really stopped, through the /stop path
    assert cancelled.is_set()
    assert [e.text for e in adapter.stop_events] == ["/stop"]
    assert adapter._active_sessions == {}

    # acked exactly once: no redelivery, no NAK, no terminate
    assert message.acked == 1 and message.nacked == 0 and message.termed == 0

    assert lifecycle_types(adapter, memo) == [
        "bloodbank.conversation.turn.started",
        "bloodbank.agent.invocation.started",
        "bloodbank.agent.invocation.failed",
        "bloodbank.conversation.turn.completed",
    ]
    failed = failed_fact(adapter, memo)["data"]
    assert failed["error_code"] == "turn_timeout"
    assert failed["lane"] == "interactive"
    assert failed["timeout_seconds"] == 0.2
    assert failed["timeout_source"] == "lane"
    assert failed["turn_stopped"] is True
    assert failed["elapsed_seconds"] >= 0.2
    assert "timed out after" in failed["error_message"]
    assert "interactive lane" in failed["error_message"]
    completed = [
        e for e in adapter._js.events if e["type"] == "bloodbank.conversation.turn.completed"
    ]
    assert completed[-1]["data"]["outcome"] == "failed"

    record = adapter.execution_state.get(memo["command_id"])
    assert record.state == "completed"
    assert record.outcome == "failure"
    assert record.failure_code == "turn_timeout"
    assert record.lane == "interactive"
    assert [e["type"] for e in record.terminal_events][0] == (
        "bloodbank.agent.invocation.failed"
    )


@pytest.mark.asyncio
async def test_timeout_frees_the_slot_so_a_third_interactive_command_runs(
    tmp_path, valid_command
):
    adapter = make_timeout_adapter(tmp_path, timeout=0.3, max_inflight=2)
    interactive = lane(adapter, "interactive")
    interactive.subscription = QueueSubscription()
    adapter.fetch_timeout_seconds = 0.05
    ran = []

    async def handler(event):
        ran.append(event.source.chat_id)
        if event.source.chat_id.startswith("hung-"):
            await asyncio.sleep(60)
        return "done"

    adapter.set_message_handler(handler)
    hung = [memo_command(valid_command, f"hung-{i}") for i in range(2)]
    hung_messages = [FakeMessage(command) for command in hung]
    for message in hung_messages:
        interactive.subscription.queue.put_nowait(message)
    third = memo_command(valid_command, "memo-third")
    third_message = FakeMessage(third)

    adapter._running = True
    loop = asyncio.create_task(adapter._consume_loop(interactive))
    try:
        for _ in range(100):
            if len(interactive.inflight) == 2:
                break
            await asyncio.sleep(0.01)
        # both slots are held by hung turns; the third is not pulled yet
        assert len(interactive.inflight) == 2
        interactive.subscription.queue.put_nowait(third_message)
        await asyncio.sleep(0.1)
        assert "memo-third" not in ran

        for _ in range(300):
            if third_message.acked:
                break
            await asyncio.sleep(0.02)
        assert third_message.acked == 1, "the freed slot never ran the third command"
        # the second hung turn expires at the same moment; give it time to settle
        for _ in range(100):
            if all(message.acked for message in hung_messages):
                break
            await asyncio.sleep(0.02)
        assert all(message.acked == 1 for message in hung_messages)
        assert "memo-third" in ran
        assert failed_fact(adapter, hung[0])["data"]["error_code"] == "turn_timeout"
        assert "bloodbank.agent.invocation.completed" in lifecycle_types(adapter, third)
        # nothing was redelivered or retried
        assert all(m.nacked == 0 and m.termed == 0 for m in hung_messages + [third_message])
    finally:
        adapter._running = False
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        await asyncio.gather(*interactive.inflight, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_turn_inside_its_budget_is_unaffected(tmp_path, valid_command):
    adapter = make_timeout_adapter(tmp_path, timeout=5)
    interactive = lane(adapter, "interactive")

    async def handler(_event):
        await asyncio.sleep(0.05)
        return "done"

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-fast")
    message = FakeMessage(memo)
    await adapter._handle_broker_message(message, interactive)

    assert message.acked == 1
    assert adapter.stop_events == []
    assert "bloodbank.agent.invocation.completed" in lifecycle_types(adapter, memo)
    record = adapter.execution_state.get(memo["command_id"])
    assert record.outcome == "success" and record.failure_code is None


@pytest.mark.asyncio
async def test_late_completion_after_timeout_is_ignored(tmp_path, valid_command):
    adapter = make_timeout_adapter(tmp_path, timeout=0.15)
    interactive = lane(adapter, "interactive")
    seen = {}

    async def handler(event):
        seen["event"] = event
        await asyncio.sleep(30)

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-late")
    message = FakeMessage(memo)
    await adapter._handle_broker_message(message, interactive)
    published = list(adapter._js.events)
    before = adapter.execution_state.get(memo["command_id"])

    # Hermes finishes the turn after the gateway gave up on it
    await adapter.on_processing_complete(
        seen["event"], __import__("gateway.platforms.base", fromlist=["x"]).ProcessingOutcome.SUCCESS
    )

    assert adapter._js.events == published
    assert adapter.execution_state.get(memo["command_id"]) == before
    assert message.acked == 1


@pytest.mark.asyncio
async def test_completion_racing_the_stop_cannot_flip_the_outcome(
    tmp_path, valid_command, monkeypatch
):
    from gateway.platforms.base import ProcessingOutcome

    adapter = make_timeout_adapter(tmp_path, timeout=0.15)
    interactive = lane(adapter, "interactive")
    seen = {}

    async def handler(event):
        seen["event"] = event
        await asyncio.sleep(30)

    adapter.set_message_handler(handler)
    real_cancel = adapter.cancel_session_processing

    async def racing_cancel(key, **kwargs):
        # a success arrives while the gateway is stopping the turn
        await adapter.on_processing_complete(seen["event"], ProcessingOutcome.SUCCESS)
        await real_cancel(key, **kwargs)

    monkeypatch.setattr(adapter, "cancel_session_processing", racing_cancel)
    memo = memo_command(valid_command, "memo-race")
    message = FakeMessage(memo)
    await adapter._handle_broker_message(message, interactive)

    types = lifecycle_types(adapter, memo)
    assert types.count("bloodbank.agent.invocation.failed") == 1
    assert "bloodbank.agent.invocation.completed" not in types
    assert adapter.execution_state.get(memo["command_id"]).failure_code == "turn_timeout"


@pytest.mark.asyncio
async def test_unconfirmed_stop_is_reported_not_hidden(tmp_path, valid_command):
    adapter = make_timeout_adapter(tmp_path, timeout=0.15)
    interactive = lane(adapter, "interactive")
    adapter.stop_ineffective = True
    release = asyncio.Event()

    async def handler(_event):
        await release.wait()

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-stuck")
    message = FakeMessage(memo)
    try:
        await asyncio.wait_for(adapter._handle_broker_message(message, interactive), 5)
        failed = failed_fact(adapter, memo)["data"]
        assert failed["error_code"] == "turn_timeout"
        assert failed["turn_stopped"] is False
        assert "did not confirm" in failed["error_message"]
        # the slot is still freed and the message settled
        assert message.acked == 1
    finally:
        release.set()
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_envelope_deadline_can_shorten_but_never_extend_the_lane_limit(
    tmp_path, valid_command
):
    adapter = make_timeout_adapter(tmp_path, timeout=0.4)
    interactive = lane(adapter, "interactive")

    async def handler(_event):
        await asyncio.sleep(30)

    adapter.set_message_handler(handler)

    short = memo_command(valid_command, "memo-short", deadline_seconds=0.1)
    await adapter._handle_broker_message(FakeMessage(short), interactive)
    data = failed_fact(adapter, short)["data"]
    assert data["timeout_source"] == "envelope" and data["timeout_seconds"] == 0.1

    long = memo_command(valid_command, "memo-long", deadline_seconds=3600)
    await adapter._handle_broker_message(FakeMessage(long), interactive)
    data = failed_fact(adapter, long)["data"]
    assert data["timeout_source"] == "lane" and data["timeout_seconds"] == 0.4


@pytest.mark.asyncio
async def test_batch_lane_has_no_wall_clock_unless_configured(tmp_path, valid_command):
    adapter = make_timeout_adapter(tmp_path, timeout=0.05)
    finished = asyncio.Event()

    async def handler(_event):
        await asyncio.sleep(0.3)
        finished.set()
        return "done"

    adapter.set_message_handler(handler)
    command = json.loads(json.dumps(valid_command))
    command["data"]["deadline_seconds"] = 0.05  # ignored: batch has no ceiling
    message = FakeMessage(command)
    await adapter._handle_broker_message(message, lane(adapter, "batch"))

    assert finished.is_set() and message.acked == 1
    assert adapter.stop_events == []
    types = [e["type"] for e in adapter._js.events]
    assert "bloodbank.agent.invocation.completed" in types


@pytest.mark.asyncio
async def test_timed_out_command_replays_its_failure_without_rerunning(
    tmp_path, valid_command
):
    adapter = make_timeout_adapter(tmp_path, timeout=0.1)
    interactive = lane(adapter, "interactive")
    runs = []

    async def handler(event):
        runs.append(event.message_id)
        await asyncio.sleep(30)

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-replay")
    first = FakeMessage(memo)
    await adapter._handle_broker_message(first, interactive)
    count = len(adapter._js.events)

    # a duplicate delivery (ack lost on the wire) must not run the prompt again
    again = FakeMessage(memo)
    await adapter._handle_broker_message(again, interactive)
    assert runs == [memo["command_id"]]
    assert again.acked == 1 and again.nacked == 0
    assert len(adapter._js.events) == count + 4  # identical facts, same Nats-Msg-Id


@pytest.mark.asyncio
async def test_failed_terminal_publish_after_timeout_naks_and_replays(
    tmp_path, valid_command
):
    adapter = make_timeout_adapter(tmp_path, timeout=0.1)
    interactive = lane(adapter, "interactive")
    runs = []

    async def handler(event):
        runs.append(event.message_id)
        await asyncio.sleep(30)

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-publish")
    real_publish = adapter._js.publish
    failures = {"left": 1}

    async def flaky(subject, payload, **kwargs):
        if subject.endswith("invocation.failed") and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("broker unavailable")
        return await real_publish(subject, payload, **kwargs)

    adapter._js.publish = flaky
    first = FakeMessage(memo)
    await adapter._handle_broker_message(first, interactive)
    assert first.nacked == 1 and first.acked == 0
    assert adapter.execution_state.get(memo["command_id"]).failure_code == "turn_timeout"

    second = FakeMessage(memo)
    await adapter._handle_broker_message(second, interactive)
    assert second.acked == 1
    assert runs == [memo["command_id"]]  # the prompt was never run twice
    assert failed_fact(adapter, memo)["data"]["error_code"] == "turn_timeout"


def test_deadline_seconds_is_validated_at_the_boundary(valid_command):
    from bloodbank_hermes_gateway.contract import CommandInvalid, INTERACTIVE, decode_command

    command = as_interactive(valid_command)
    for good in (None, 5, 0.5):
        command["data"]["deadline_seconds"] = good
        assert decode_command(json.dumps(command).encode(), lane=INTERACTIVE)
    for bad in (0, -3, True, "20", float("inf"), [], {}):
        command["data"]["deadline_seconds"] = bad
        with pytest.raises(CommandInvalid, match="deadline_seconds"):
            decode_command(json.dumps(command, allow_nan=True).encode(), lane=INTERACTIVE)


def test_journal_gains_a_nullable_failure_code_column_without_losing_rows(tmp_path):
    path = tmp_path / "state.sqlite3"
    ExecutionStateStore(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE executions DROP COLUMN failure_code")
        db.execute(
            """INSERT INTO executions(command_id, envelope_digest, profile, state,
               started_events, lane, created_at, updated_at)
               VALUES ('old', 'd', 'p', 'pending', '[]', 'interactive', 't', 't')"""
        )
    store = ExecutionStateStore(path)
    row = store.get("old")
    assert row is not None and row.failure_code is None and row.lane == "interactive"


def test_bloodbank_turns_never_auto_voice(tmp_path):
    """voice.auto_tts is a fleet-wide default for human chats; a Bloodbank command turn must never synthesize."""
    adapter = make_adapter(tmp_path)
    adapter._auto_tts_default = True  # what the gateway syncs from voice.auto_tts at startup
    assert adapter._should_auto_tts_for_chat("a9ffc5c2-memo-thread") is False


# -- agent.invocation.progressed: interim assistant messages of a running command turn


def _interim(command_id):
    """Metadata Hermes' stream consumer puts on a completed commentary send (_send_commentary)."""
    return {"reply_to_message_id": command_id, "_interim_send": True}


def _progressed(adapter):
    return [e for e in adapter._js.events if e["type"] == "bloodbank.agent.invocation.progressed"]


def test_adapter_declares_no_message_editing():
    """Hermes streams tokens only to editing adapters; without editing, a commentary arrives as one anchored send."""
    assert BloodbankAdapter.SUPPORTS_MESSAGE_EDITING is False


@pytest.mark.asyncio
async def test_interim_messages_of_a_running_turn_become_progress_facts(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    command_id = valid_command["command_id"]
    thread = valid_command["data"]["thread_id"]
    valid_command["data"]["context"] = {"memo_id": "m-1", "reason": "voice-inbox"}
    sent = []

    async def handler(event):
        sent.append(await adapter.send(thread, "  Checking the date with a shell command.  ", metadata=_interim(command_id)))
        sent.append(await adapter.send(thread, "It is Tuesday; writing the answer.", metadata=_interim(command_id)))
        return "It is Tuesday."

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)

    assert message.acked == 1
    assert all(r.success for r in sent)
    assert [e["type"] for e in adapter._js.events] == [
        "bloodbank.conversation.turn.started",
        "bloodbank.agent.invocation.started",
        "bloodbank.agent.invocation.progressed",
        "bloodbank.agent.invocation.progressed",
        "bloodbank.agent.invocation.completed",
        "bloodbank.conversation.turn.completed",
    ]
    first, second = _progressed(adapter)
    started = adapter._js.events[1]
    for seq, event in ((1, first), (2, second)):
        assert event["subject"] == "bloodbank.evt.agent.invocation.progressed"
        assert event["correlationid"] == valid_command["correlationid"]
        assert event["causationid"] == started["id"]
        d = event["data"]
        assert d["seq"] == seq
        assert d["command_id"] == command_id
        assert d["target_agent_id"] == "bloodbank-pm"
        assert d["lane"] == "batch"
        assert d["context"] == {"memo_id": "m-1", "reason": "voice-inbox"}
        assert d["elapsed_seconds"] >= 0
        assert d["text_truncated"] is False
    assert first["data"]["text"] == "Checking the date with a shell command."
    assert second["data"]["text"] == "It is Tuesday; writing the answer."
    assert first["id"] != second["id"]


@pytest.mark.asyncio
async def test_final_reply_status_notices_and_blank_text_publish_no_progress(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    command_id = valid_command["command_id"]
    thread = valid_command["data"]["thread_id"]

    async def handler(event):
        await adapter.send(thread, "   \n ", metadata=_interim(command_id))  # blank commentary
        await adapter.send(thread, None, metadata=_interim(command_id))  # not text at all
        # the "still working" heartbeat and background-review notes: interim, but not anchored to the command
        await adapter.send(thread, "⏳ Still working (5 min)", metadata={"_interim_send": True})
        # a tool-progress bubble: neither interim nor anchored
        await adapter.send(thread, "💻 date", metadata=None)
        # another command's anchor on this chat
        await adapter.send(thread, "not ours", metadata=_interim(str(uuid.uuid4())))
        return "The final answer."  # the conftest gateway delivers it with a plain send (no metadata)

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)

    assert message.acked == 1
    assert _progressed(adapter) == []
    assert [e["type"] for e in adapter._js.events][-2:] == [
        "bloodbank.agent.invocation.completed",
        "bloodbank.conversation.turn.completed",
    ]


@pytest.mark.asyncio
async def test_send_for_an_unknown_chat_or_after_the_turn_never_crashes_or_publishes(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    command_id = valid_command["command_id"]
    thread = valid_command["data"]["thread_id"]

    # nothing running at all: not our chat
    result = await adapter.send("somebody-else", "hello", metadata=_interim(command_id))
    assert result.success is True
    assert (await adapter.send("somebody-else", "hello")).success is True
    assert (await adapter.send("somebody-else", "hello", metadata="not a mapping")).success is True

    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result="done"))
    await adapter._handle_broker_message(FakeMessage(valid_command))
    # the turn is over: a late commentary for it is not progress
    assert (await adapter.send(thread, "late commentary", metadata=_interim(command_id))).success is True
    assert _progressed(adapter) == []


@pytest.mark.asyncio
async def test_a_progress_publish_failure_never_fails_the_send_or_the_turn(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    command_id = valid_command["command_id"]
    thread = valid_command["data"]["thread_id"]
    real_publish = adapter._js.publish

    async def flaky(subject, payload, **kwargs):
        if subject.endswith(".progressed"):
            raise RuntimeError("stream unavailable")
        return await real_publish(subject, payload, **kwargs)

    adapter._js.publish = flaky
    results = []

    async def handler(event):
        results.append(await adapter.send(thread, "Looking it up.", metadata=_interim(command_id)))
        results.append(await adapter.send(thread, "Still looking.", metadata=_interim(command_id)))
        return "done"

    adapter.set_message_handler(handler)
    message = FakeMessage(valid_command)
    await adapter._handle_broker_message(message)
    assert [r.success for r in results] == [True, True]
    assert message.acked == 1
    assert adapter._js.events[-2]["type"] == "bloodbank.agent.invocation.completed"


@pytest.mark.asyncio
async def test_progress_text_is_cut_to_500_characters_and_marked(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    command_id = valid_command["command_id"]
    thread = valid_command["data"]["thread_id"]

    async def handler(event):
        await adapter.send(thread, "word " * 300, metadata=_interim(command_id))
        return "done"

    adapter.set_message_handler(handler)
    await adapter._handle_broker_message(FakeMessage(valid_command))
    (event,) = _progressed(adapter)
    assert len(event["data"]["text"]) <= 500
    assert event["data"]["text"].endswith("…")
    assert event["data"]["text_truncated"] is True


@pytest.mark.asyncio
async def test_progress_seq_is_per_command_and_lane_is_stamped(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    thread = valid_command["data"]["thread_id"]

    async def handler(event):
        await adapter.send(thread, "step", metadata=_interim(event.message_id))
        return "done"

    adapter.set_message_handler(handler)
    await adapter._handle_broker_message(FakeMessage(valid_command))
    second = json.loads(json.dumps(valid_command))
    second["id"] = str(uuid.uuid4())
    second["command_id"] = str(uuid.uuid4())
    second["idempotency_key"] = f"agent.invocation.start:turn:{second['command_id']}"
    await adapter._handle_broker_message(FakeMessage(second))
    events = _progressed(adapter)
    assert [(e["data"]["command_id"], e["data"]["seq"]) for e in events] == [
        (valid_command["command_id"], 1),
        (second["command_id"], 1),
    ]


# --- Hermes drains and refusals ---------------------------------------------
#
# While Hermes drains for a reload/restart it keeps the adapter connected and
# answers every new message with "Gateway is restarting and is not accepting new
# work right now." through send(), reported as a SUCCESS turn. None of that may
# close a command: the lanes stop pulling, and anything already in hand is NAKed
# with its journal row left dispatchable.

DRAIN_REFUSAL = "⏳ Gateway is restarting and is not accepting new work right now."
SHUTDOWN_REFUSAL = "⏳ Gateway is shutting down and is not accepting new work right now."
RUNNER_FLAGS = {
    "restart": "_restart_requested",
    "draining": "_draining",
    "external": "_external_drain_active",
}


def make_runner(**on):
    flags = {attr: bool(on.get(name)) for name, attr in RUNNER_FLAGS.items()}
    return SimpleNamespace(_running=True, **flags)


class JsMessage(FakeMessage):
    """A FakeMessage with JetStream delivery metadata and recorded NAK delays."""

    def __init__(self, envelope, *, delivered=1):
        super().__init__(envelope)
        self.metadata = SimpleNamespace(num_delivered=delivered)
        self.nak_delays = []

    async def nak(self, delay=None):
        self.nak_delays.append(delay)
        self.nacked += 1


def terminal_types(adapter, command):
    return [
        t
        for t in lifecycle_types(adapter, command)
        if t in ("bloodbank.agent.invocation.completed", "bloodbank.agent.invocation.failed")
    ]


def lane_and_command(adapter, valid_command, name):
    if name == "batch":
        return lane(adapter, "batch"), valid_command
    return lane(adapter, "interactive"), as_interactive(valid_command)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", sorted(RUNNER_FLAGS))
async def test_no_lane_fetches_while_hermes_refuses_new_turns_and_both_resume_after(
    tmp_path, flag, caplog
):
    caplog.set_level("INFO", logger="bloodbank_hermes_gateway.adapter")
    adapter = make_lane_adapter(tmp_path)
    adapter.drain_poll_seconds = 0.001
    runner = make_runner(**{flag: True})
    adapter.gateway_runner = runner
    fetches = []

    class Subscription:
        def __init__(self, name):
            self.name = name

        async def fetch(self, *_args, **_kwargs):
            fetches.append(self.name)
            return []

    for item in adapter.lanes:
        item.subscription = Subscription(item.name)

    for _ in range(25):
        for item in adapter.lanes:
            await adapter._consume_once(item)
    assert fetches == []
    paused = [r for r in caplog.records if "stopped fetching" in r.getMessage()]
    assert len(paused) == 2  # once per lane, not once per poll

    setattr(runner, RUNNER_FLAGS[flag], False)
    for item in adapter.lanes:
        await adapter._consume_once(item)
    assert sorted(fetches) == ["batch", "interactive"]
    assert any("resumed fetching" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_thirty_minute_drain_spends_no_delivery_and_the_command_runs_after(
    tmp_path, valid_command
):
    """The broker only counts a delivery when a lane pulls; a paused lane never does."""

    adapter = make_adapter(tmp_path)
    adapter.drain_poll_seconds = 0.001
    adapter.fetch_timeout_seconds = 0.005
    runner = make_runner(restart=True)
    adapter.gateway_runner = runner
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result="done"))
    queued = [JsMessage(valid_command, delivered=0)]
    delivered = []

    class Broker:
        async def fetch(self, *_args, timeout=None, **_kwargs):
            if not queued:
                await asyncio.sleep(timeout or 0)
                raise TimeoutError
            message = queued.pop(0)
            message.metadata.num_delivered += 1
            delivered.append(message)
            return [message]

    adapter._subscription = Broker()
    adapter._running = True
    loop = asyncio.create_task(adapter._consume_loop())
    try:
        # hundreds of poll cycles of "drain": still nothing delivered
        await asyncio.sleep(0.3)
        assert delivered == []
        runner._restart_requested = False
        for _ in range(500):
            if delivered and delivered[0].acked:
                break
            await asyncio.sleep(0.005)
    finally:
        adapter._running = False
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    (message,) = delivered
    assert message.metadata.num_delivered == 1
    assert message.acked == 1 and message.nacked == 0
    assert terminal_types(adapter, valid_command) == ["bloodbank.agent.invocation.completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lane_name", ["batch", "interactive"])
async def test_a_command_fetched_just_before_the_drain_is_nakked_and_runs_after(
    tmp_path, valid_command, lane_name
):
    adapter = make_lane_adapter(tmp_path)
    lane_runtime, command = lane_and_command(adapter, valid_command, lane_name)
    adapter.gateway_runner = make_runner(restart=True)
    calls = []

    async def handler(event):
        calls.append(event.message_id)
        return "done"

    adapter.set_message_handler(handler)
    message = JsMessage(command)
    await adapter._handle_broker_message(message, lane_runtime)

    assert calls == []
    assert (message.acked, message.nacked, message.termed) == (0, 1, 0)
    assert message.nak_delays == [lane_runtime.nak_delay]
    # nothing published (not even started) and the row is still dispatchable
    assert adapter._js.events == []
    assert adapter.execution_state.get(command["command_id"]).state == "pending"

    adapter.gateway_runner = make_runner()  # the new process
    again = JsMessage(command, delivered=2)
    await adapter._handle_broker_message(again, lane_runtime)
    assert calls == [command["command_id"]]
    assert again.acked == 1 and again.nacked == 0
    assert terminal_types(adapter, command) == ["bloodbank.agent.invocation.completed"]


@pytest.mark.asyncio
async def test_a_drain_that_starts_while_started_facts_publish_is_never_dispatched(
    tmp_path, valid_command
):
    adapter = make_adapter(tmp_path)
    runner = make_runner()
    adapter.gateway_runner = runner
    real_publish = adapter._js.publish

    async def publish(subject, payload, **kwargs):
        await real_publish(subject, payload, **kwargs)
        runner._draining = True  # SIGUSR1 lands mid-publication

    adapter._js.publish = publish
    calls = []
    adapter.set_message_handler(lambda event: calls.append(event) or asyncio.sleep(0, result="x"))
    message = JsMessage(valid_command)
    await adapter._handle_broker_message(message)

    assert calls == []
    assert message.nacked == 1 and message.acked == 0
    assert terminal_types(adapter, valid_command) == []
    # started may have escaped; the row says so and stays dispatchable
    assert adapter.execution_state.get(valid_command["command_id"]).state == "started"


@pytest.mark.asyncio
@pytest.mark.parametrize("lane_name", ["batch", "interactive"])
@pytest.mark.parametrize(
    "refusal",
    [
        DRAIN_REFUSAL,
        SHUTDOWN_REFUSAL,
        "⏳ Gateway is restarting and is not accepting another turn right now.",
        "Hermes is at the active session limit (8/8). Try again when another session finishes.",
        # a runtime footer after the whole notice is still the notice
        DRAIN_REFUSAL + "\n\n`gpt-5 · 0.1s`",
        "⚠️ This session's history is temporarily unavailable, so this message was not "
        "processed. Ask the operator to inspect state.db, then resend after it is healthy.",
    ],
)
async def test_a_turn_hermes_refused_is_not_success_and_reruns_on_redelivery(
    tmp_path, valid_command, lane_name, refusal
):
    adapter = make_lane_adapter(tmp_path)
    lane_runtime, command = lane_and_command(adapter, valid_command, lane_name)
    runner = make_runner()
    adapter.gateway_runner = runner
    calls = []

    async def handler(event):
        calls.append(event.message_id)
        if len(calls) == 1:
            # the drain begins after the adapter's last look: Hermes refuses
            runner._restart_requested = True
            return refusal
        return "The real answer."

    adapter.set_message_handler(handler)
    message = JsMessage(command)
    await asyncio.wait_for(adapter._handle_broker_message(message, lane_runtime), 2)

    assert (message.acked, message.nacked, message.termed) == (0, 1, 0)
    assert terminal_types(adapter, command) == []
    assert not any(e["type"].endswith(".turn.completed") for e in adapter._js.events)
    row = adapter.execution_state.get(command["command_id"])
    assert row.state == "started" and row.outcome is None
    assert adapter._records == {}

    runner._restart_requested = False
    again = JsMessage(command, delivered=2)
    await asyncio.wait_for(adapter._handle_broker_message(again, lane_runtime), 2)
    assert len(calls) == 2
    assert again.acked == 1
    assert terminal_types(adapter, command) == ["bloodbank.agent.invocation.completed"]
    assert adapter.execution_state.get(command["command_id"]).outcome == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "code", "key"),
    [
        (
            "⚠️ Something went wrong and I couldn't finish this reply. You are being "
            "rate-limited. Please wait a moment and try again.\nUse /retry to try again, or "
            "/new to start a fresh conversation. Technical details are in the gateway log "
            "(`hermes logs`).\n\nThis turn did not complete. Some actions may already have "
            "run; verify their effects before resending.",
            "hermes_turn_error",
            "gateway.errors.generic_failed_with_hint",
        ),
        (
            "⏱️ The AI model service is rate-limiting requests. Wait a moment, then use /retry.",
            "hermes_turn_error",
            "gateway.errors.rate_limited",
        ),
        (
            "⏳ Another turn is still running on this session. To protect the transcript, this "
            "message was not processed. Wait for the active turn to finish, then resend it.",
            "hermes_refused",
            "gateway.busy.another_turn_running",
        ),
        (
            "Unknown command `/frobnicate`. Type /commands to see what's available, or resend "
            "without the leading slash to send as a regular message.",
            "hermes_refused",
            "gateway.unknown_command",
        ),
        (
            # only sent with busy_input_mode queue/steer: Hermes kept its own copy,
            # so a NAK would run the command twice
            "⏳ Gateway restarting — queued for the next turn after it comes back.",
            "hermes_refused",
            "gateway.busy.drain_queued",
        ),
        # the gateway.errors.* notices the first catalog missed
        (
            "⚠️ Session storage was temporarily unavailable, so this turn was stopped to "
            "protect your conversation history. Your message should already be saved — "
            "please send it again.",
            "hermes_turn_error",
            "gateway.errors.session_storage_unavailable",
        ),
        (
            "⚠️ Session storage was temporarily unavailable, so this turn was stopped to "
            "protect your conversation history. Please check available disk space, then "
            "send your message again.",
            "hermes_turn_error",
            "gateway.errors.session_storage_unavailable_disk",
        ),
        (
            "⚠️ This conversation has grown too long for me to read all at once. Use "
            "/compress to shorten the history, or /new to start a fresh conversation.",
            "hermes_turn_error",
            "gateway.errors.context_overflow",
        ),
        (
            "⚠️ I had to stop before finishing: Response truncated due to output length "
            "limit. Use /retry to try again, or /compress if this conversation has grown "
            "very long.",
            "hermes_turn_error",
            "gateway.errors.stopped_before_finishing",
        ),
        (
            "⚠️ The AI model service rejected this request. Try rephrasing your message, "
            "or use /model to switch models.",
            "hermes_turn_error",
            "gateway.errors.bad_request",
        ),
        (
            "⚠️ Hermes could not reach the AI model service (no further detail from the "
            "SDK). Use /retry to try again; if it persists, run `hermes doctor` on the host.",
            "hermes_turn_error",
            "gateway.errors.connection_unknown",
        ),
        (
            "⚠️ The model returned only a silence marker for a message that needed a "
            "reply. Try again or rephrase.",
            "hermes_turn_error",
            "gateway.errors.unexpected_silence",
        ),
        (
            # the agent loop's own abandonment line (Hermes' inactivity stop, say)
            "Operation interrupted: waiting for model response (12.3s elapsed).",
            "hermes_turn_error",
            "agent.operation_interrupted",
        ),
    ],
)
async def test_an_error_notice_in_place_of_an_answer_closes_as_an_explicit_failure(
    tmp_path, valid_command, reply, code, key
):
    adapter = make_adapter(tmp_path)
    adapter.gateway_runner = make_runner()
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result=reply))
    message = JsMessage(valid_command)
    await adapter._handle_broker_message(message)

    assert (message.acked, message.nacked) == (1, 0)
    assert terminal_types(adapter, valid_command) == ["bloodbank.agent.invocation.failed"]
    data = failed_fact(adapter, valid_command)["data"]
    assert data["error_code"] == code
    assert data["refusal"] == key
    assert key in data["error_message"]
    turn = [e for e in adapter._js.events if e["type"].endswith(".turn.completed")]
    assert turn[-1]["data"]["outcome"] == "failed"
    row = adapter.execution_state.get(valid_command["command_id"])
    assert (row.state, row.outcome, row.failure_code) == ("completed", "failure", code)


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before_dispatch", "refused_by_hermes", "not_admitted"])
async def test_on_its_last_delivery_a_command_hermes_did_not_run_is_closed_as_failed(
    tmp_path, valid_command, when
):
    """A NAK on the last delivery would drop the command with no terminal fact at all."""

    adapter = make_adapter(tmp_path)
    runner = make_runner(restart=when == "before_dispatch")
    adapter.gateway_runner = runner
    adapter.drop_inbound = when == "not_admitted"
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result=DRAIN_REFUSAL))
    message = JsMessage(valid_command, delivered=adapter.max_deliver)
    await asyncio.wait_for(adapter._handle_broker_message(message), 2)

    assert (message.acked, message.nacked) == (1, 0)
    assert lifecycle_types(adapter, valid_command) == [
        "bloodbank.conversation.turn.started",
        "bloodbank.agent.invocation.started",
        "bloodbank.agent.invocation.failed",
        "bloodbank.conversation.turn.completed",
    ]
    data = failed_fact(adapter, valid_command)["data"]
    assert data["error_code"] == "hermes_refused"
    assert data["refusal"] == {
        "before_dispatch": "gateway_restarting",
        "refused_by_hermes": "gateway.busy.drain_rejected_new_work",
        "not_admitted": "not_accepted",
    }[when]
    assert "no deliveries left" in data["error_message"]
    row = adapter.execution_state.get(valid_command["command_id"])
    assert (row.state, row.outcome, row.failure_code) == ("completed", "failure", "hermes_refused")


@pytest.mark.asyncio
async def test_an_event_hermes_does_not_admit_is_nakked_instead_of_waiting_forever(
    tmp_path, valid_command
):
    adapter = make_adapter(tmp_path)  # batch: no wall clock, it used to hang here
    adapter.drop_inbound = True
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result="done"))
    message = JsMessage(valid_command)
    await asyncio.wait_for(adapter._handle_broker_message(message), 1)
    assert (message.acked, message.nacked) == (0, 1)
    assert terminal_types(adapter, valid_command) == []
    assert adapter.execution_state.get(valid_command["command_id"]).state == "started"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "done",
        # a real answer that talks about a drain is still an answer
        "I checked: earlier the gateway said it is restarting and is not accepting new "
        "work right now, but it is back.",
        "Gateway status: running.",
        # opens with a NOT_RUN notice's words, then answers: an answer, never a retry
        "Hermes is at the active session limit (8/8) is what the board said an hour ago. "
        "Since then the queue drained and I groomed POOL-12: acceptance criteria added, "
        "estimate set to 3, and the dependency on POOL-9 recorded. " * 2,
        "⚠️ Your message wasn't processed (the previous turn was still being cleaned up). "
        "Please send it again. — that is what Hermes told me last time; this time I "
        "finished the job and here is the full summary of what changed on the board " * 3,
    ],
)
async def test_normal_turns_are_unaffected_by_the_refusal_checks(tmp_path, valid_command, answer):
    adapter = make_adapter(tmp_path)
    adapter.gateway_runner = make_runner()
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result=answer))
    message = JsMessage(valid_command)
    await adapter._handle_broker_message(message)
    assert (message.acked, message.nacked) == (1, 0)
    assert terminal_types(adapter, valid_command) == ["bloodbank.agent.invocation.completed"]
    assert adapter.execution_state.get(valid_command["command_id"]).outcome == "success"


@pytest.mark.asyncio
async def test_a_turn_already_running_when_the_drain_begins_still_completes(
    tmp_path, valid_command
):
    """Draining waits for in-flight turns; their real answers close them as success."""

    adapter = make_adapter(tmp_path)
    runner = make_runner()
    adapter.gateway_runner = runner
    running = asyncio.Event()
    release = asyncio.Event()

    async def handler(_event):
        running.set()
        await release.wait()
        return "Finished the grooming."

    adapter.set_message_handler(handler)
    message = JsMessage(valid_command)
    task = asyncio.create_task(adapter._handle_broker_message(message))
    await asyncio.wait_for(running.wait(), 1)
    runner._restart_requested = runner._draining = True
    # Hermes warns active chats about the shutdown with an unanchored send
    await adapter.send(valid_command["data"]["thread_id"], DRAIN_REFUSAL)
    release.set()
    await asyncio.wait_for(task, 1)
    assert (message.acked, message.nacked) == (1, 0)
    assert terminal_types(adapter, valid_command) == ["bloodbank.agent.invocation.completed"]


def test_notices_are_read_from_hermes_catalog_when_it_is_importable():
    from bloodbank_hermes_gateway.refusals import FAILED, NOT_RUN, NoticeClassifier

    german = {
        "gateway.busy.action_restarting": "wird neu gestartet",
        "gateway.busy.action_shutting_down": "fährt herunter",
        "gateway.busy.drain_rejected_new_work": "⏳ Gateway {action} und nimmt gerade keine neue Arbeit an.",
        # a gateway.errors key the gateway has never heard of is still a failure
        "gateway.errors.brand_new_failure": "⚠️ Der Dienst hat etwas völlig Neues falsch gemacht.",
        "gateway.errors.hint_brand_new": " Ein Hinweis, der nie allein gesendet wird.",
    }
    notices = NoticeClassifier([german])
    found = notices.classify("⏳ Gateway wird neu gestartet und nimmt gerade keine neue Arbeit an.")
    assert found is not None and found.key == "gateway.busy.drain_rejected_new_work"
    assert found.disposition == NOT_RUN
    new = notices.classify("⚠️ Der Dienst hat etwas völlig Neues falsch gemacht.")
    assert new is not None and (new.key, new.disposition) == ("gateway.errors.brand_new_failure", FAILED)
    assert notices.classify(" Ein Hinweis, der nie allein gesendet wird.") is None
    # the English copy still matches when the catalog lacks a key
    assert notices.classify(DRAIN_REFUSAL).disposition == NOT_RUN
    unknown = (
        "Unknown command `/x`. Type /commands to see what's available, or resend without "
        "the leading slash to send as a regular message."
    )
    assert notices.classify(unknown).disposition == FAILED
    assert notices.classify("Unknown command `/x`.") is None
    for answer in (None, "", "   ", "done", 42, "⏳", "Gateway is restarting"):
        assert notices.classify(answer) is None
    # catalog lookups that blow up fall back to the English copies
    def broken():
        raise RuntimeError("no catalog")

    assert NoticeClassifier(broken).classify(DRAIN_REFUSAL).disposition == NOT_RUN


def _hermes_locale():
    """Hermes' bundled English catalog, flattened, when a Hermes checkout is on this host."""

    import os
    from pathlib import Path

    yaml = pytest.importorskip("yaml")
    root = Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes/hermes-agent"))
    path = root / "locales" / "en.yaml"
    if not path.is_file():
        pytest.skip(f"no Hermes catalog at {path}")
    flat = {}

    def walk(node, prefix):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{prefix}.{key}" if prefix else str(key))
        elif isinstance(node, str):
            flat[prefix] = node

    walk(yaml.safe_load(path.read_text(encoding="utf-8")), "")
    return flat


def test_every_hermes_error_notice_is_classified_never_success():
    """A gateway.errors.* key that falls through would close its command as success."""

    from string import Formatter

    from bloodbank_hermes_gateway.refusals import (
        NOT_RUN_ERRORS,
        NoticeClassifier,
        disposition_for,
        is_fragment_key,
    )

    catalog = _hermes_locale()
    keys = [k for k in catalog if k.startswith("gateway.errors.") and not is_fragment_key(k)]
    assert len(keys) >= 20
    notices = NoticeClassifier([catalog])
    for key in keys:
        template = catalog[key]
        fields = {f for _l, f, _s, _c in Formatter().parse(template) if f}
        reply = template.format(**{f: " (detail)" for f in fields})
        found = notices.classify(reply)
        assert found is not None, key
        assert found.key == key, (key, found.key)
        assert found.disposition == disposition_for(key)
    assert NOT_RUN_ERRORS <= set(keys)
    # the fragments really are fragments, not replies
    assert {"gateway.errors.processing_incomplete", "gateway.errors.hint_quota"} <= {
        k for k in catalog if is_fragment_key(k)
    }


# --- Turns Hermes' stop() cuts off -------------------------------------------
#
# When the stop drain runs out (restart_drain_timeout on SIGTERM, or after a
# reload's restart_after_turn_timeout wait) Hermes hard-interrupts every turn
# still running through the runner's _interrupt_running_agents. The turn then
# returns its partial text (or "Operation interrupted ...", or nothing) and the
# base adapter reports SUCCESS; a turn still alive at adapter teardown is
# CANCELLED. Neither finished, so neither may close as success (or cancelled).


def stopping_runner(adapter, *, restart=True):
    """A runner with an interruptible agent registry, as Hermes wires it."""

    runner = make_runner()
    runner._running_agents = {}
    runner.interrupts = []
    runner._interrupt_running_agents = lambda reason: runner.interrupts.append(reason)
    adapter.gateway_runner = runner

    def begin_stop():
        runner._running = False
        runner._draining = True
        runner._restart_requested = restart

    runner.begin_stop = begin_stop
    return runner


async def _running_turn(adapter, command, lane_runtime=None, reply="partial"):
    running = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def handler(event):
        calls.append(event.message_id)
        if len(calls) == 1:
            running.set()
            await release.wait()
            return reply
        return "The real answer."

    adapter.set_message_handler(handler)
    message = JsMessage(command)
    task = asyncio.create_task(adapter._handle_broker_message(message, lane_runtime))
    await asyncio.wait_for(running.wait(), 1)
    return message, task, release, calls


def _session_of(adapter, command):
    record = adapter._records[command["command_id"]]
    return adapter._session_key(record.event)


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [True, False])
@pytest.mark.parametrize(
    "reply",
    [
        "I groomed the acceptance criteria and was about to set",
        "Operation interrupted: waiting for model response (61.8s elapsed).",
        None,  # an interrupted run that did work and stays silent
    ],
)
async def test_a_turn_the_stop_drain_cut_off_closes_as_interrupted_never_success(
    tmp_path, valid_command, reply, restart
):
    adapter = make_adapter(tmp_path)
    runner = stopping_runner(adapter, restart=restart)
    message, task, release, _calls = await _running_turn(adapter, valid_command, reply=reply)

    runner.begin_stop()
    runner._running_agents[_session_of(adapter, valid_command)] = object()
    runner._interrupt_running_agents("Gateway restarting" if restart else "Gateway shutting down")
    # the wrapper forwards to Hermes' own interrupt
    assert runner.interrupts == ["Gateway restarting" if restart else "Gateway shutting down"]
    release.set()
    await asyncio.wait_for(task, 1)

    assert (message.acked, message.nacked) == (1, 0)
    assert terminal_types(adapter, valid_command) == ["bloodbank.agent.invocation.failed"]
    data = failed_fact(adapter, valid_command)["data"]
    kind = "restart" if restart else "shutdown"
    assert data["error_code"] == "interrupted_by_shutdown"
    assert data["interrupted_by"] == f"gateway_{kind}"
    assert data["elapsed_seconds"] >= 0
    assert ("restarting" if restart else "shutting down") in data["error_message"]
    assert "partial work may have run" in data["error_message"]
    row = adapter.execution_state.get(valid_command["command_id"])
    assert (row.state, row.outcome, row.failure_code) == (
        "completed",
        "failure",
        "interrupted_by_shutdown",
    )


@pytest.mark.asyncio
async def test_a_turn_cancelled_at_adapter_teardown_during_stop_is_interrupted_not_cancelled(
    tmp_path, valid_command
):
    adapter = make_adapter(tmp_path)
    runner = make_runner()  # no interrupt hook at all: the fallback alone decides
    adapter.gateway_runner = runner
    message, task, _release, _calls = await _running_turn(adapter, valid_command)

    runner._running = False
    runner._draining = runner._restart_requested = True
    await adapter.cancel_background_tasks()  # Hermes' adapter teardown
    await asyncio.wait_for(task, 1)

    assert (message.acked, message.nacked) == (1, 0)
    data = failed_fact(adapter, valid_command)["data"]
    assert data["error_code"] == "interrupted_by_shutdown"
    assert "Hermes reported cancelled" in data["error_message"]


@pytest.mark.asyncio
async def test_an_interrupted_line_after_stop_began_is_interrupted_without_the_hook(
    tmp_path, valid_command
):
    adapter = make_adapter(tmp_path)
    runner = make_runner()
    adapter.gateway_runner = runner
    message, task, release, _calls = await _running_turn(
        adapter, valid_command, reply="Operation interrupted: waiting for model response (3.0s elapsed)."
    )
    runner._running = False
    runner._draining = True
    release.set()
    await asyncio.wait_for(task, 1)
    data = failed_fact(adapter, valid_command)["data"]
    assert (data["error_code"], data["interrupted_by"]) == ("interrupted_by_shutdown", "gateway_shutdown")


@pytest.mark.asyncio
async def test_a_turn_that_finished_inside_the_stop_drain_is_still_success(
    tmp_path, valid_command
):
    """The live proof's command 1: its answer landed 0.16 s after stop() began."""

    adapter = make_adapter(tmp_path)
    runner = stopping_runner(adapter)
    message, task, release, _calls = await _running_turn(
        adapter, valid_command, reply="drain test 1 done"
    )
    runner.begin_stop()  # _running False, but nothing is interrupted
    release.set()
    await asyncio.wait_for(task, 1)
    assert (message.acked, message.nacked) == (1, 0)
    assert terminal_types(adapter, valid_command) == ["bloodbank.agent.invocation.completed"]
    assert adapter.execution_state.get(valid_command["command_id"]).outcome == "success"


@pytest.mark.asyncio
async def test_the_interrupt_only_marks_turns_hermes_is_still_running(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    runner = stopping_runner(adapter)
    message, task, release, _calls = await _running_turn(
        adapter, valid_command, reply="Groomed: criteria, estimate, dependency."
    )
    runner.begin_stop()
    # another session timed out the drain; this one already finished in Hermes
    runner._running_agents["someone-else:thread-9"] = object()
    runner._interrupt_running_agents("Gateway restarting")
    release.set()
    await asyncio.wait_for(task, 1)
    assert terminal_types(adapter, valid_command) == ["bloodbank.agent.invocation.completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lane_name", ["batch", "interactive"])
async def test_retry_mode_naks_a_cut_off_turn_and_the_next_process_reruns_its_thread(
    tmp_path, valid_command, lane_name
):
    adapter = make_lane_adapter(tmp_path)
    lane_runtime, command = lane_and_command(adapter, valid_command, lane_name)
    lane_runtime.shutdown_interrupt = "retry"
    runner = stopping_runner(adapter)
    message, task, release, calls = await _running_turn(adapter, command, lane_runtime)

    runner.begin_stop()
    runner._running_agents[_session_of(adapter, command)] = object()
    runner._interrupt_running_agents("Gateway restarting")
    release.set()
    await asyncio.wait_for(task, 1)

    assert (message.acked, message.nacked) == (0, 1)
    assert message.nak_delays == [lane_runtime.nak_delay]
    assert terminal_types(adapter, command) == []
    row = adapter.execution_state.get(command["command_id"])
    assert row.state == "started" and row.outcome is None

    # the new process: same command, same thread, runs for real
    adapter.gateway_runner = stopping_runner(adapter)
    again = JsMessage(command, delivered=2)
    await asyncio.wait_for(adapter._handle_broker_message(again, lane_runtime), 2)
    assert calls == [command["command_id"]] * 2
    assert again.acked == 1
    assert terminal_types(adapter, command) == ["bloodbank.agent.invocation.completed"]


@pytest.mark.asyncio
async def test_retry_mode_on_the_last_delivery_closes_as_interrupted(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    adapter._batch.shutdown_interrupt = "retry"
    runner = stopping_runner(adapter)
    running = asyncio.Event()
    release = asyncio.Event()

    async def handler(_event):
        running.set()
        await release.wait()
        return "partial"

    adapter.set_message_handler(handler)
    message = JsMessage(valid_command, delivered=adapter.max_deliver)
    task = asyncio.create_task(adapter._handle_broker_message(message))
    await asyncio.wait_for(running.wait(), 1)
    runner.begin_stop()
    runner._running_agents[_session_of(adapter, valid_command)] = object()
    runner._interrupt_running_agents("Gateway restarting")
    release.set()
    await asyncio.wait_for(task, 1)
    assert (message.acked, message.nacked) == (1, 0)
    data = failed_fact(adapter, valid_command)["data"]
    assert data["error_code"] == "interrupted_by_shutdown"
    assert data["interrupted_by"] == "gateway_restart"
    assert "no deliveries left" in data["error_message"]


def test_the_runner_interrupt_is_wrapped_once_for_every_adapter(tmp_path):
    first = make_adapter(tmp_path / "a")
    second = make_adapter(tmp_path / "b")
    runner = make_runner()
    runner.interrupts = []
    runner._interrupt_running_agents = lambda reason: runner.interrupts.append(reason)
    first.gateway_runner = second.gateway_runner = runner
    first._watch_runner_interrupts()
    wrapped = runner._interrupt_running_agents
    second._watch_runner_interrupts()
    first._watch_runner_interrupts()
    assert runner._interrupt_running_agents is wrapped
    runner._interrupt_running_agents("Gateway shutting down")
    assert runner.interrupts == ["Gateway shutting down"]
    # a runner without the method is left alone
    bare = make_runner()
    first.gateway_runner = bare
    first._watch_runner_interrupts()
    assert not hasattr(bare, "_interrupt_running_agents")


def test_shutdown_interrupt_mode_is_strict(tmp_path):
    adapter = make_lane_adapter(tmp_path)
    assert {item.shutdown_interrupt for item in adapter.lanes} == {"fail"}
    adapter.config.extra["shutdown_interrupt"] = "retry"
    adapter.config.extra["interactive"]["shutdown_interrupt"] = "retry"
    built = BloodbankAdapter(adapter.config)
    assert {item.shutdown_interrupt for item in built.lanes} == {"retry"}
    for bad in ("nak", True, None):
        adapter.config.extra["shutdown_interrupt"] = bad
        with pytest.raises(ValueError, match="shutdown_interrupt"):
            BloodbankAdapter(adapter.config)


@pytest.mark.asyncio
async def test_an_unadmitted_slash_prompt_is_closed_not_redelivered(tmp_path, valid_command):
    """A bypass slash command runs inline on a busy session: a redelivery would repeat it."""

    command = json.loads(json.dumps(valid_command))
    command["data"]["prompt"] = "/new"
    adapter = make_adapter(tmp_path)
    adapter.drop_inbound = True
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result="done"))
    message = JsMessage(command)  # first delivery: plenty left
    await asyncio.wait_for(adapter._handle_broker_message(message), 1)
    assert (message.acked, message.nacked) == (1, 0)
    data = failed_fact(adapter, command)["data"]
    assert (data["error_code"], data["refusal"]) == ("hermes_refused", "not_accepted_slash_command")
    assert "no deliveries left" not in data["error_message"]


@pytest.mark.asyncio
async def test_a_pause_that_lasts_is_logged_again(tmp_path, caplog):
    caplog.set_level("INFO", logger="bloodbank_hermes_gateway.adapter")
    adapter = make_adapter(tmp_path)
    adapter.drain_poll_seconds = 0.001
    adapter.pause_remind_seconds = 0.02
    adapter.gateway_runner = make_runner(external=True)
    for _ in range(3):
        await adapter._consume_once(adapter._batch)
    await asyncio.sleep(0.03)
    await adapter._consume_once(adapter._batch)
    messages = [r.getMessage() for r in caplog.records]
    assert sum("stopped fetching" in m for m in messages) == 1
    assert sum("still not fetching" in m and "maintenance" in m for m in messages) == 1


# --- Detach at the interactive wall clock ------------------------------------
#
# extra.interactive.detach_on_timeout: a turn that outlives the lane limit gives
# its slot back instead of being stopped. It keeps running (message un-acked,
# progress acks, journal row 'started' + detached_at, conversation lock held),
# publishes agent.invocation.detached once, and closes with exactly one
# completed/failed fact carrying detached: true. Only a turn still running at
# detached_ceiling seconds after dispatch is stopped (turn_timeout,
# timeout_source detached_ceiling).


def make_detach_adapter(tmp_path, *, timeout=0.15, ceiling=5.0, max_detached=2, max_inflight=2):
    adapter = make_lane_adapter(tmp_path, max_inflight=max_inflight)
    adapter.config.extra["interactive"].update(
        detach_on_timeout=True,
        max_detached=max_detached,
        turn_timeout_seconds=timeout,
        detached_ceiling_seconds=ceiling,
    )
    adapter = BloodbankAdapter(adapter.config)
    adapter._js = FakeJetStream()
    for item in adapter.lanes:
        item.ack_progress = 0.01
    return adapter


def detached_facts(adapter, command):
    return [
        event
        for event in adapter._js.events
        if event["data"]["command_id"] == command["command_id"]
        and event["type"] == "bloodbank.agent.invocation.detached"
    ]


def completed_fact(adapter, command):
    return next(
        event
        for event in adapter._js.events
        if event["data"]["command_id"] == command["command_id"]
        and event["type"] == "bloodbank.agent.invocation.completed"
    )


def test_detach_is_off_by_default_and_its_configuration_is_strict(tmp_path):
    interactive = lane(make_lane_adapter(tmp_path), "interactive")
    assert interactive.detach_on_timeout is False
    assert (interactive.max_detached, interactive.detached_ceiling) == (0, None)
    assert interactive.max_ack_pending == interactive.max_inflight == 2

    adapter = make_adapter(tmp_path)
    adapter.config.extra["interactive"] = {"enabled": True, "detach_on_timeout": True}
    built = lane(BloodbankAdapter(adapter.config), "interactive")
    assert (built.turn_timeout, built.max_detached, built.detached_ceiling) == (300.0, 2, 2700.0)
    # the durable must hold the slots plus the detached turns' un-acked messages
    assert built.max_ack_pending == 4

    for bad in ({"detach_on_timeout": "yes"}, {"detach_on_timeout": 1}):
        adapter.config.extra["interactive"] = {"enabled": True, **bad}
        with pytest.raises(ValueError, match="detach_on_timeout"):
            BloodbankAdapter(adapter.config)
    for bad in (0, 65, "many"):
        adapter.config.extra["interactive"] = {
            "enabled": True, "detach_on_timeout": True, "max_detached": bad,
        }
        with pytest.raises(ValueError, match="max_detached"):
            BloodbankAdapter(adapter.config)
    for bad in (0, -5, True, "2700", float("nan"), 86_401):
        adapter.config.extra["interactive"] = {
            "enabled": True, "detach_on_timeout": True, "detached_ceiling_seconds": bad,
        }
        with pytest.raises(ValueError, match="detached_ceiling_seconds"):
            BloodbankAdapter(adapter.config)
    # a ceiling at or under the lane limit would detach nothing
    adapter.config.extra["interactive"] = {
        "enabled": True, "detach_on_timeout": True,
        "turn_timeout_seconds": 300, "detached_ceiling_seconds": 300,
    }
    with pytest.raises(ValueError, match="greater than turn_timeout_seconds"):
        BloodbankAdapter(adapter.config)


def test_the_consumer_contract_counts_detached_turns(tmp_path):
    adapter = make_detach_adapter(tmp_path)
    interactive = lane(adapter, "interactive")
    durable = SimpleNamespace(
        filter_subject="bloodbank.cmd.agent.invocation.invoke",
        ack_policy=SimpleNamespace(value="explicit"),
        max_ack_pending=4,
        max_deliver=interactive.max_deliver,
    )
    adapter._assert_consumer_contract(durable, interactive)
    assert adapter._needs_ack_pending_update(durable, interactive) is False

    old = SimpleNamespace(**{**vars(durable), "max_ack_pending": 2})
    with pytest.raises(RuntimeError, match="does not match"):
        adapter._assert_consumer_contract(old, interactive)
    # only max_ack_pending differs: updated in place on connect
    assert adapter._needs_ack_pending_update(old, interactive) is True
    # anything else wrong is still refused, never "fixed"
    wrong = SimpleNamespace(**{**vars(old), "filter_subject": "bloodbank.cmd.other"})
    assert adapter._needs_ack_pending_update(wrong, interactive) is False
    wrong = SimpleNamespace(**{**vars(old), "max_deliver": 99})
    assert adapter._needs_ack_pending_update(wrong, interactive) is False


@pytest.mark.asyncio
async def test_connect_raises_an_existing_durables_max_ack_pending_for_detach(
    tmp_path, monkeypatch
):
    adapter = make_detach_adapter(tmp_path)
    durables = {
        "bloodbank-hermes-gateway": SimpleNamespace(
            filter_subject="bloodbank.cmd.agent.invocation.start",
            ack_policy=SimpleNamespace(value="explicit"),
            max_ack_pending=2,
            max_deliver=5,
        ),
        "bloodbank-hermes-gateway-interactive": SimpleNamespace(
            filter_subject="bloodbank.cmd.agent.invocation.invoke",
            ack_policy=SimpleNamespace(value="explicit"),
            max_ack_pending=2,  # bound before detach existed
            max_deliver=3,
        ),
    }
    updates = []
    never = asyncio.Event()

    class Subscription:
        async def fetch(self, *_args, **_kwargs):
            await never.wait()

    class JetStream:
        async def pull_subscribe(self, subject, **kwargs):
            # an existing durable is bound as it is: pull_subscribe never edits it
            return Subscription()

        async def consumer_info(self, _stream, durable, timeout=None):
            return SimpleNamespace(config=durables[durable])

        async def add_consumer(self, stream, config=None, timeout=None):
            updates.append((stream, config))
            durables[config.durable_name].max_ack_pending = config.max_ack_pending
            return SimpleNamespace(config=config)

    class Connection:
        def jetstream(self):
            return JetStream()

        async def drain(self):
            return None

    async def connect(**_kwargs):
        return Connection()

    nats_module = types.ModuleType("nats")
    nats_module.__path__ = []
    nats_module.connect = connect
    nats_js = types.ModuleType("nats.js")
    nats_js.__path__ = []
    nats_api = types.ModuleType("nats.js.api")
    nats_api.AckPolicy = SimpleNamespace(EXPLICIT="explicit")
    nats_api.DeliverPolicy = SimpleNamespace(ALL="all")
    nats_api.ConsumerConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "nats", nats_module)
    monkeypatch.setitem(sys.modules, "nats.js", nats_js)
    monkeypatch.setitem(sys.modules, "nats.js.api", nats_api)

    assert await adapter.connect() is True
    assert [(stream, config.durable_name, config.max_ack_pending) for stream, config in updates] == [
        ("BLOODBANK_COMMANDS", "bloodbank-hermes-gateway-interactive", 4)
    ]
    assert durables["bloodbank-hermes-gateway"].max_ack_pending == 2  # batch untouched
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_a_detached_turn_runs_on_and_closes_completed_once_as_a_late_reply(
    tmp_path, valid_command
):
    adapter = make_detach_adapter(tmp_path, timeout=0.1)
    interactive = lane(adapter, "interactive")
    release = asyncio.Event()

    async def handler(_event):
        await release.wait()
        return "The answer, late."

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-long")
    message = JsMessage(memo)
    task = asyncio.create_task(adapter._handle_broker_message(message, interactive))
    for _ in range(200):
        if detached_facts(adapter, memo):
            break
        await asyncio.sleep(0.01)

    # past the lane limit: detached, not stopped, nothing terminal, nothing acked
    assert adapter.stop_events == []
    assert terminal_types(adapter, memo) == []
    assert (message.acked, message.nacked, message.termed) == (0, 0, 0)
    assert task in interactive.detached  # the running handler holds no slot
    record = adapter._records[memo["command_id"]]
    assert record.detached_at is not None and not record.finalizing
    row = adapter.execution_state.get(memo["command_id"])
    assert row.state == "started" and row.detached_at is not None
    [fact] = detached_facts(adapter, memo)
    assert fact["subject"] == "bloodbank.evt.agent.invocation.detached"
    assert fact["data"]["lane"] == "interactive"
    assert fact["data"]["timeout_seconds"] == 0.1
    assert fact["data"]["timeout_source"] == "lane"
    assert fact["data"]["ceiling_seconds"] == 5.0
    assert fact["data"]["elapsed_seconds"] >= 0.1
    assert fact["data"]["context"] == memo["data"]["context"]
    # progress acks keep the un-acked message alive while it runs on
    progress_before = message.progress
    await asyncio.sleep(0.05)
    assert message.progress > progress_before

    release.set()
    await asyncio.wait_for(task, 2)

    assert (message.acked, message.nacked, message.termed) == (1, 0, 0)
    assert terminal_types(adapter, memo) == ["bloodbank.agent.invocation.completed"]
    data = completed_fact(adapter, memo)["data"]
    assert data["detached"] is True
    assert data["detached_after_seconds"] >= 0.1
    assert data["elapsed_seconds"] >= data["detached_after_seconds"]
    assert data["lane"] == "interactive"
    row = adapter.execution_state.get(memo["command_id"])
    assert (row.state, row.outcome, row.failure_code) == ("completed", "success", None)
    assert row.detached_at is not None
    assert memo["command_id"] not in adapter._records
    assert interactive.detached == set()
    assert adapter._conversation_locks == {}


@pytest.mark.asyncio
async def test_a_turn_inside_its_budget_never_detaches(tmp_path, valid_command):
    adapter = make_detach_adapter(tmp_path, timeout=5, ceiling=10)
    adapter.set_message_handler(lambda _event: asyncio.sleep(0.02, result="done"))
    memo = memo_command(valid_command, "memo-quick")
    message = FakeMessage(memo)
    await adapter._handle_broker_message(message, lane(adapter, "interactive"))
    assert message.acked == 1 and detached_facts(adapter, memo) == []
    data = completed_fact(adapter, memo)["data"]
    assert "detached" not in data and "elapsed_seconds" not in data
    assert adapter.execution_state.get(memo["command_id"]).detached_at is None


@pytest.mark.asyncio
async def test_detaching_frees_the_slot_so_the_next_memo_runs_while_both_run_on(
    tmp_path, valid_command
):
    adapter = make_detach_adapter(tmp_path, timeout=0.2, max_inflight=2)
    interactive = lane(adapter, "interactive")
    interactive.subscription = QueueSubscription()
    adapter.fetch_timeout_seconds = 0.05
    release = asyncio.Event()
    ran = []

    async def handler(event):
        ran.append(event.source.chat_id)
        if event.source.chat_id.startswith("long-"):
            await release.wait()
        return "done"

    adapter.set_message_handler(handler)
    long = [memo_command(valid_command, f"long-{i}") for i in range(2)]
    long_messages = [FakeMessage(command) for command in long]
    for message in long_messages:
        interactive.subscription.queue.put_nowait(message)
    third = memo_command(valid_command, "memo-third")
    third_message = FakeMessage(third)

    adapter._running = True
    loop = asyncio.create_task(adapter._consume_loop(interactive))
    try:
        for _ in range(100):
            if len(interactive.inflight) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(interactive.inflight) == 2
        interactive.subscription.queue.put_nowait(third_message)
        await asyncio.sleep(0.1)
        assert "memo-third" not in ran  # both slots held, inside the limit

        for _ in range(300):
            if third_message.acked:
                break
            await asyncio.sleep(0.02)
        assert third_message.acked == 1, "the slot a detached turn gave back never ran the third memo"
        # both long turns run on, detached, un-acked, never stopped (their detached
        # facts go out once the journal has the stamp, and only while they run on)
        assert len(interactive.detached) == 2
        for _ in range(200):
            if all(detached_facts(adapter, c) for c in long):
                break
            await asyncio.sleep(0.01)
        assert all(m.acked == 0 and m.nacked == 0 for m in long_messages)
        assert adapter.stop_events == []
        assert all(terminal_types(adapter, c) == [] for c in long)

        release.set()
        for _ in range(100):
            if all(m.acked for m in long_messages):
                break
            await asyncio.sleep(0.02)
        assert all(m.acked == 1 and m.nacked == 0 for m in long_messages)
        for command in long:
            assert terminal_types(adapter, command) == ["bloodbank.agent.invocation.completed"]
            assert completed_fact(adapter, command)["data"]["detached"] is True
            assert len(detached_facts(adapter, command)) == 1
        assert "detached" not in completed_fact(adapter, third)["data"]
        assert interactive.detached == set()
    finally:
        adapter._running = False
        release.set()
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        await asyncio.gather(*interactive.inflight, *interactive.detached, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_detached_turn_still_running_at_the_ceiling_is_stopped(
    tmp_path, valid_command
):
    adapter = make_detach_adapter(tmp_path, timeout=0.1, ceiling=0.3)
    interactive = lane(adapter, "interactive")
    cancelled = asyncio.Event()

    async def handler(_event):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-forever")
    message = FakeMessage(memo)
    await asyncio.wait_for(adapter._handle_broker_message(message, interactive), 5)

    assert cancelled.is_set()
    assert [e.text for e in adapter.stop_events] == ["/stop"]
    assert (message.acked, message.nacked) == (1, 0)
    assert len(detached_facts(adapter, memo)) == 1
    assert terminal_types(adapter, memo) == ["bloodbank.agent.invocation.failed"]
    data = failed_fact(adapter, memo)["data"]
    assert data["error_code"] == "turn_timeout"
    assert data["timeout_source"] == "detached_ceiling"
    assert data["timeout_seconds"] == 0.3
    assert data["elapsed_seconds"] >= 0.3
    assert data["detached"] is True and data["detached_after_seconds"] >= 0.1
    assert data["turn_stopped"] is True
    assert "detached at" in data["error_message"] and "ceiling 0.3s" in data["error_message"]
    row = adapter.execution_state.get(memo["command_id"])
    assert (row.outcome, row.failure_code) == ("failure", "turn_timeout")
    assert interactive.detached == set()


@pytest.mark.asyncio
async def test_with_max_detached_reached_the_turn_is_stopped_as_before(
    tmp_path, valid_command
):
    adapter = make_detach_adapter(tmp_path, timeout=0.1, max_detached=1)
    interactive = lane(adapter, "interactive")
    release = asyncio.Event()

    async def handler(event):
        if event.source.chat_id == "first":
            await release.wait()
            return "done"
        await asyncio.sleep(30)

    adapter.set_message_handler(handler)
    first = memo_command(valid_command, "first")
    first_message = FakeMessage(first)
    first_task = asyncio.create_task(adapter._handle_broker_message(first_message, interactive))
    for _ in range(200):
        if detached_facts(adapter, first):
            break
        await asyncio.sleep(0.01)
    assert len(interactive.detached) == 1

    second = memo_command(valid_command, "second")
    second_message = FakeMessage(second)
    await asyncio.wait_for(adapter._handle_broker_message(second_message, interactive), 5)
    assert detached_facts(adapter, second) == []
    assert failed_fact(adapter, second)["data"]["timeout_source"] == "lane"
    assert "detached" not in failed_fact(adapter, second)["data"]
    assert second_message.acked == 1

    release.set()
    await asyncio.wait_for(first_task, 2)
    assert terminal_types(adapter, first) == ["bloodbank.agent.invocation.completed"]


@pytest.mark.asyncio
async def test_an_envelope_deadline_still_stops_the_turn(tmp_path, valid_command):
    """data.deadline_seconds is the caller asking for a stop: it never detaches."""

    adapter = make_detach_adapter(tmp_path, timeout=5, ceiling=10)

    async def handler(_event):
        await asyncio.sleep(30)

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-deadline", deadline_seconds=0.1)
    message = FakeMessage(memo)
    await asyncio.wait_for(adapter._handle_broker_message(message, lane(adapter, "interactive")), 5)
    assert detached_facts(adapter, memo) == []
    data = failed_fact(adapter, memo)["data"]
    assert (data["timeout_source"], data["timeout_seconds"]) == ("envelope", 0.1)
    assert message.acked == 1


async def _detached_turn_cut_off_by_a_reload(adapter, interactive, memo):
    runner = stopping_runner(adapter)
    message, task, release, _calls = await _running_turn(adapter, memo, interactive)
    for _ in range(200):
        if detached_facts(adapter, memo):
            break
        await asyncio.sleep(0.01)
    assert interactive.detached

    runner.begin_stop()
    runner._running_agents[_session_of(adapter, memo)] = object()
    runner._interrupt_running_agents("Gateway restarting")
    release.set()
    await asyncio.wait_for(task, 2)
    return message


@pytest.mark.asyncio
async def test_a_detached_turn_a_reload_cuts_off_closes_failed_and_is_never_rerun(
    tmp_path, valid_command
):
    """Review 2026-10-22: a NAK re-run of a detached turn redid up to 45 min of work
    (and its side-effecting tools) after the caller heard "still on it". Even with
    shutdown_interrupt: retry on the lane, a detached turn closes as failed."""

    adapter = make_detach_adapter(tmp_path, timeout=0.1)
    interactive = lane(adapter, "interactive")
    interactive.shutdown_interrupt = "retry"  # what the live config sets on both lanes
    assert interactive.detached_shutdown_interrupt == "fail"
    memo = memo_command(valid_command, "memo-reload")
    message = await _detached_turn_cut_off_by_a_reload(adapter, interactive, memo)

    assert (message.acked, message.nacked) == (1, 0)
    assert terminal_types(adapter, memo) == ["bloodbank.agent.invocation.failed"]
    data = failed_fact(adapter, memo)["data"]
    assert data["error_code"] == "interrupted_by_shutdown"
    assert data["detached"] is True and data["detached_after_seconds"] >= 0.1
    row = adapter.execution_state.get(memo["command_id"])
    assert (row.state, row.outcome, row.failure_code) == ("completed", "failure", "interrupted_by_shutdown")
    assert interactive.detached == set()


@pytest.mark.asyncio
async def test_a_detached_turn_a_reload_cuts_off_is_nakked_when_configured_to_retry(
    tmp_path, valid_command
):
    adapter = make_detach_adapter(tmp_path, timeout=0.1)
    interactive = lane(adapter, "interactive")
    interactive.detached_shutdown_interrupt = "retry"
    memo = memo_command(valid_command, "memo-reload")
    message = await _detached_turn_cut_off_by_a_reload(adapter, interactive, memo)

    # no terminal fact, journal still dispatchable: the next process re-runs it
    assert (message.acked, message.nacked) == (0, 1)
    assert terminal_types(adapter, memo) == []
    row = adapter.execution_state.get(memo["command_id"])
    assert row.state == "started" and row.detached_at is not None
    assert interactive.detached == set()

    # the re-run is a new attempt: it starts with no detached_at of its own
    adapter.gateway_runner = make_runner()  # the next process: accepting again
    adapter._js.events.clear()
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result="done"))
    again = JsMessage(memo, delivered=2)
    seen = []
    real_mark_started = adapter.execution_state.mark_started

    def spy(**kwargs):
        record = real_mark_started(**kwargs)
        seen.append(record.detached_at)
        return record

    adapter.execution_state.mark_started = spy
    await asyncio.wait_for(adapter._handle_broker_message(again, interactive), 2)
    assert seen == [None]
    assert again.acked == 1
    assert "detached" not in completed_fact(adapter, memo)["data"]


def test_detached_shutdown_interrupt_is_strict(tmp_path):
    adapter = make_adapter(tmp_path)
    adapter.config.extra["interactive"] = {"enabled": True, "detach_on_timeout": True}
    assert lane(BloodbankAdapter(adapter.config), "interactive").detached_shutdown_interrupt == "fail"
    adapter.config.extra["interactive"]["detached_shutdown_interrupt"] = "retry"
    assert lane(BloodbankAdapter(adapter.config), "interactive").detached_shutdown_interrupt == "retry"
    adapter.config.extra["interactive"]["detached_shutdown_interrupt"] = "nak"
    with pytest.raises(ValueError, match="detached_shutdown_interrupt"):
        BloodbankAdapter(adapter.config)


@pytest.mark.asyncio
async def test_a_turn_that_closes_while_its_detach_is_journaled_publishes_no_detached_fact(
    tmp_path, valid_command, monkeypatch
):
    """Review 2026-10-22: the detached fact went out AFTER the completed one (a slow
    journal write at load), and the journal never got detached_at."""

    adapter = make_detach_adapter(tmp_path, timeout=0.1)
    interactive = lane(adapter, "interactive")
    finish = asyncio.Event()
    real_mark_detached = adapter.execution_state.mark_detached
    loop = asyncio.get_running_loop()

    def slow_mark_detached(**kwargs):
        loop.call_soon_threadsafe(finish.set)  # the turn ends during the journal write
        time.sleep(0.2)
        return real_mark_detached(**kwargs)

    monkeypatch.setattr(adapter.execution_state, "mark_detached", slow_mark_detached)

    async def handler(_event):
        await finish.wait()
        return "done"

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-race")
    message = FakeMessage(memo)
    await asyncio.wait_for(adapter._handle_broker_message(message, interactive), 3)

    types_seen = [e["type"] for e in adapter._js.events if e["data"]["command_id"] == memo["command_id"]]
    assert "bloodbank.agent.invocation.detached" not in types_seen
    assert terminal_types(adapter, memo) == ["bloodbank.agent.invocation.completed"]
    assert completed_fact(adapter, memo)["data"]["detached"] is True
    assert message.acked == 1


def test_a_new_attempt_of_a_started_command_clears_the_last_attempts_detached_at(tmp_path):
    store = ExecutionStateStore(tmp_path / "state.sqlite3")
    store.claim_pending(command_id="c1", digest="d", profile="p", started_events=())
    store.mark_started(command_id="c1", digest="d")
    assert store.mark_detached(command_id="c1", digest="d").detached_at is not None
    # a redelivery after a NAK or an unclean exit: dispatched again
    again = store.mark_started(command_id="c1", digest="d")
    assert again.state == "started" and again.detached_at is None
    assert store.get("c1").detached_at is None
    # and an attempt that detaches stamps its own time
    assert store.mark_detached(command_id="c1", digest="d").detached_at is not None


@pytest.mark.asyncio
async def test_a_failed_turn_after_detach_closes_failed_once_with_detached(
    tmp_path, valid_command
):
    adapter = make_detach_adapter(tmp_path, timeout=0.1)
    interactive = lane(adapter, "interactive")
    release = asyncio.Event()

    async def handler(_event):
        await release.wait()
        raise RuntimeError("tool exploded")

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-fails-late")
    message = FakeMessage(memo)
    task = asyncio.create_task(adapter._handle_broker_message(message, interactive))
    for _ in range(200):
        if detached_facts(adapter, memo):
            break
        await asyncio.sleep(0.01)
    release.set()
    await asyncio.wait_for(task, 2)
    assert terminal_types(adapter, memo) == ["bloodbank.agent.invocation.failed"]
    data = failed_fact(adapter, memo)["data"]
    assert data["error_code"] == "processing_failed" and data["detached"] is True
    assert message.acked == 1


@pytest.mark.asyncio
async def test_disconnect_settles_detached_turns_too(tmp_path, valid_command):
    adapter = make_detach_adapter(tmp_path, timeout=0.1)
    interactive = lane(adapter, "interactive")

    async def handler(_event):
        await asyncio.sleep(30)

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-teardown")
    message = FakeMessage(memo)
    task = asyncio.create_task(adapter._handle_broker_message(message, interactive))
    interactive.inflight.add(task)
    for _ in range(200):
        if detached_facts(adapter, memo):
            break
        await asyncio.sleep(0.01)
    assert task in interactive.detached and task not in interactive.inflight

    await asyncio.wait_for(adapter.disconnect(), 10)
    assert task.done()
    # settled one way or the other (cancelled turn closes, or the handler NAKs); never left hanging
    assert message.acked + message.nacked == 1
    assert interactive.detached == set()


@pytest.mark.asyncio
async def test_a_detached_journal_or_publish_failure_never_fails_the_turn(
    tmp_path, valid_command, monkeypatch
):
    adapter = make_detach_adapter(tmp_path, timeout=0.1)
    interactive = lane(adapter, "interactive")
    release = asyncio.Event()

    def broken_mark_detached(**_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(adapter.execution_state, "mark_detached", broken_mark_detached)
    real_publish = adapter._js.publish

    async def flaky(subject, payload, **kwargs):
        if subject.endswith("invocation.detached"):
            raise RuntimeError("broker unavailable")
        return await real_publish(subject, payload, **kwargs)

    adapter._js.publish = flaky

    async def handler(_event):
        await release.wait()
        return "done"

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-blind")
    message = FakeMessage(memo)
    task = asyncio.create_task(adapter._handle_broker_message(message, interactive))
    for _ in range(200):
        if adapter._records.get(memo["command_id"]) and adapter._records[memo["command_id"]].detached_at:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait_for(task, 2)
    assert message.acked == 1
    assert completed_fact(adapter, memo)["data"]["detached"] is True


def test_journal_gains_a_nullable_detached_at_column_without_losing_rows(tmp_path):
    path = tmp_path / "state.sqlite3"
    ExecutionStateStore(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE executions DROP COLUMN detached_at")
        db.execute(
            """INSERT INTO executions(command_id, envelope_digest, profile, state,
                   started_events, created_at, updated_at)
               VALUES ('old', 'digest', 'research', 'started', '[]', 'now', 'now')"""
        )
    store = ExecutionStateStore(path)
    row = store.get("old")
    assert row.state == "started" and row.detached_at is None
    detached = store.mark_detached(command_id="old", digest="digest")
    assert detached.detached_at is not None and detached.state == "started"
    # idempotent: the first stamp is kept
    assert store.mark_detached(command_id="old", digest="digest").detached_at == detached.detached_at
    with pytest.raises(RuntimeError, match="different command envelope"):
        store.mark_detached(command_id="old", digest="other")


# --- data.model: one turn on another model route -----------------------------


@pytest.mark.asyncio
async def test_data_model_switches_only_that_turn_through_model_once(tmp_path, valid_command):
    adapter = make_timeout_adapter(tmp_path, timeout=5)
    adapter.model_switch_delay = 0.05
    seen = []

    async def handler(event):
        seen.append((event.text, getattr(event, "turn_model", None)))
        return "done"

    adapter.set_message_handler(handler)
    memo = memo_command(valid_command, "memo-fast-model", model="automaticai/auto/fast")
    message = FakeMessage(memo)
    await adapter._handle_broker_message(message, lane(adapter, "interactive"))

    [switch] = adapter.model_events
    assert switch.text == "/model automaticai/auto/fast --once"
    assert switch.internal is True
    assert switch.message_id == f"{memo['command_id']}:turn-model"
    assert switch.source.chat_id == "memo-fast-model"
    # the prompt ran after the switch finished, on the switched model, once
    assert seen == [(memo["data"]["prompt"], "automaticai/auto/fast")]
    assert message.acked == 1
    data = completed_fact(adapter, memo)["data"]
    assert data["model_override"] == {"requested": "automaticai/auto/fast", "applied": True}
    # the /model confirmation is not mistaken for the turn's answer or progress
    assert not any(e["type"].endswith("progressed") for e in adapter._js.events)

    # the next turn on the same thread, without data.model, is back on the profile's model
    again = memo_command(valid_command, "memo-fast-model")
    await adapter._handle_broker_message(FakeMessage(again), lane(adapter, "interactive"))
    assert seen[-1] == (again["data"]["prompt"], None)
    assert len(adapter.model_events) == 1
    assert "model_override" not in completed_fact(adapter, again)["data"]


@pytest.mark.asyncio
async def test_a_refused_model_route_leaves_the_turn_on_the_profile_model(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    adapter.model_reject = True
    seen = []

    async def handler(event):
        seen.append(getattr(event, "turn_model", None))
        return "done"

    adapter.set_message_handler(handler)
    command = json.loads(json.dumps(valid_command))
    command["data"]["model"] = "automaticai/nope"
    message = FakeMessage(command)
    await adapter._handle_broker_message(message)
    assert seen == [None] and message.acked == 1
    override = completed_fact(adapter, command)["data"]["model_override"]
    assert override["requested"] == "automaticai/nope"
    assert override["applied"] is False and override["reason"] == "rejected"
    assert "not available" in override["detail"]


@pytest.mark.asyncio
async def test_model_override_can_be_switched_off(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    adapter.config.extra["allow_model_override"] = False
    adapter = BloodbankAdapter(adapter.config)
    adapter._js = FakeJetStream()
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result="done"))
    command = json.loads(json.dumps(valid_command))
    command["data"]["model"] = "automaticai/auto/fast"
    await adapter._handle_broker_message(FakeMessage(command))
    assert adapter.model_events == []
    assert completed_fact(adapter, command)["data"]["model_override"] == {
        "requested": "automaticai/auto/fast", "applied": False, "reason": "disabled",
    }
    adapter.config.extra["allow_model_override"] = "yes"
    with pytest.raises(ValueError, match="allow_model_override"):
        BloodbankAdapter(adapter.config)


@pytest.mark.asyncio
async def test_a_slow_model_switch_is_bounded_and_the_turn_still_runs(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    adapter.model_switch_budget_seconds = 0.05
    adapter.model_switch_delay = 0.5
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result="done"))
    command = json.loads(json.dumps(valid_command))
    command["data"]["model"] = "automaticai/auto/fast"
    message = FakeMessage(command)
    try:
        await asyncio.wait_for(adapter._handle_broker_message(message), 3)
        override = completed_fact(adapter, command)["data"]["model_override"]
        assert override["applied"] is False and override["reason"] == "switch_timeout"
        # the busy session queued nothing as text: the turn ran (here the fake runs it alongside)
        assert message.acked == 1
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_the_runner_view_decides_whether_the_model_was_applied(tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    overrides = {}
    runner = make_runner()
    runner._session_key_for_source = lambda source: f"agent:{source.profile}:{source.chat_id}"
    runner._session_model_override = lambda key: overrides.get(key)
    adapter.gateway_runner = runner
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result="done"))

    command = json.loads(json.dumps(valid_command))
    command["data"]["model"] = "automaticai/personal/claude-sonnet-5.5"
    # Hermes confirmed, but its session holds no override: not applied
    await adapter._handle_broker_message(FakeMessage(command))
    assert completed_fact(adapter, command)["data"]["model_override"]["applied"] is False

    overrides["agent:bloodbank-pm:thread-1"] = {"model": "personal/claude-sonnet-5.5"}
    second = json.loads(json.dumps(command))
    second["command_id"] = str(uuid.uuid4())
    await adapter._handle_broker_message(FakeMessage(second))
    assert completed_fact(adapter, second)["data"]["model_override"]["applied"] is True


def _disarming_runner(adapter):
    """A runner whose settlement restore drops the session's armed --once switch."""

    runner = make_runner()
    runner.restored = []
    runner._session_key_for_source = lambda source: f"{source.profile}:{source.chat_id}"

    def restore(session_key):
        runner.restored.append(session_key)
        adapter.session_models.pop(session_key, None)

    runner._restore_pending_one_turn_model_override = restore
    adapter.gateway_runner = runner
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["drain", "route"])
async def test_a_turn_refused_after_its_model_switch_does_not_leave_the_switch_armed(
    tmp_path, valid_command, refusal
):
    """Review 2026-10-22: /model --once is restored only in a turn's finally. A
    command refused after the switch ran no turn, so the switch stayed armed and
    the thread's next turn ran on the route without asking for it."""

    adapter = make_adapter(tmp_path)
    runner = _disarming_runner(adapter)
    seen = []

    async def handler(event):
        seen.append(getattr(event, "turn_model", None))
        return "done"

    adapter.set_message_handler(handler)
    command = json.loads(json.dumps(valid_command))
    command["data"]["model"] = "automaticai/auto/fast"
    real_apply = adapter._apply_turn_model

    async def apply_then_refuse(invocation, source):
        outcome = await real_apply(invocation, source)
        assert adapter.session_models, "the switch is armed for the next turn"
        if refusal == "drain":
            runner._draining = True  # an external drain begins during the switch
        else:
            adapter.profile_resolver.resolve = lambda _target: "someone-else"
        return outcome

    adapter._apply_turn_model = apply_then_refuse
    message = FakeMessage(command)
    await adapter._handle_broker_message(message)

    assert seen == []  # no turn ran
    assert adapter.session_models == {}, "the --once switch is disarmed"
    assert runner.restored == [f"bloodbank-pm:{command['data']['thread_id']}"]
    assert message.acked == 0  # NAKed (drain) or terminated (route), never done
    assert message.nacked + message.termed == 1

    # the thread's next turn, with no data.model, runs on the profile's model
    runner._draining = False
    adapter.profile_resolver.resolve = lambda _target: "bloodbank-pm"
    adapter._apply_turn_model = real_apply
    nxt = json.loads(json.dumps(valid_command))
    nxt["command_id"] = str(uuid.uuid4())
    await adapter._handle_broker_message(FakeMessage(nxt))
    assert seen == [None]


def test_data_model_is_validated_at_the_boundary(valid_command):
    from bloodbank_hermes_gateway.contract import CommandInvalid, INTERACTIVE, decode_command

    command = as_interactive(valid_command)
    for good in (None, "automaticai/auto/fast", "automaticai/personal/claude-sonnet-5.5", "kimi-2.8"):
        command["data"]["model"] = good
        envelope = decode_command(json.dumps(command).encode(), lane=INTERACTIVE)
        assert Invocation.from_envelope(envelope, "bloodbank-pm", "interactive").model == good
    for bad in ("", " auto", "auto --global", "--global", "/model x", "a" * 201, 5, True, [], {}):
        command["data"]["model"] = bad
        with pytest.raises(CommandInvalid, match="data.model"):
            decode_command(json.dumps(command).encode(), lane=INTERACTIVE)
