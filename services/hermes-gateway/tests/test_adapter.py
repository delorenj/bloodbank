from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

from bloodbank_hermes_gateway.adapter import BloodbankAdapter


class FakeMessage:
    def __init__(self, envelope):
        self.data = json.dumps(envelope).encode()
        self.acked = 0
        self.termed = 0
        self.nacked = 0
        self.progress = 0

    async def ack(self):
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


def make_adapter(tmp_path):
    config = SimpleNamespace(
        enabled=True,
        extra={
            "target_profiles": {"bloodbank-pm": "bloodbank-pm"},
            "fleet_registry": str(tmp_path / "missing-registry.yaml"),
            "max_inflight": 2,
            "ack_wait_seconds": 10,
            "ack_progress_seconds": 1,
        },
    )
    adapter = BloodbankAdapter(config)
    adapter._js = FakeJetStream()
    adapter.ack_progress_seconds = 0.01
    return adapter


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


@pytest.mark.asyncio
async def test_connect_binds_one_bounded_durable_pull_consumer(
    tmp_path, monkeypatch
):
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
                raise asyncio.TimeoutError
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

    unknown = json.loads(json.dumps(valid_command))
    unknown["command_id"] = "f482fce5-8d0f-43cc-9f93-ab94624637a8"
    unknown["data"]["target_agent_id"] = "unknown-agent"
    unknown_message = FakeMessage(unknown)
    await adapter._handle_broker_message(unknown_message)
    assert unknown_message.termed == 1
    assert unknown_message.acked == 0


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
