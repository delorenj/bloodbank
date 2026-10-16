from __future__ import annotations

import asyncio
import json
import sqlite3
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
