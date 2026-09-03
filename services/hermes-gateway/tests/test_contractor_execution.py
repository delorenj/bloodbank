"""Focused contract tests for stateless Hermes contractor execution."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from bloodbank_hermes_gateway.adapter import BloodbankAdapter
from bloodbank_hermes_gateway.contract import (
    CommandInvalid,
    ContractorContext,
    ProfileResolver,
    RouteInvalid,
    decode_command,
)
from bloodbank_hermes_gateway.execution_state import ExecutionStateStore

from test_adapter import FakeMessage, make_adapter


def contractor_context(**overrides):
    value = {
        "type": "contractor",
        "contractor_id": "board-cranker",
        "contractor_version": 1,
        "memory_policy": "none",
        "continuity": False,
        "required_skills": ["bloodbank-integration", "coding-strategy"],
    }
    value.update(overrides)
    return value


def contractor_command(valid_command, **context_overrides):
    command = copy.deepcopy(valid_command)
    command["data"]["thread_id"] = None
    command["data"]["turn_id"] = None
    command["data"]["context"] = contractor_context(**context_overrides)
    return command


def redeliver_as_new_command(command):
    delivered = copy.deepcopy(command)
    delivered["id"] = str(uuid.uuid4())
    delivered["command_id"] = str(uuid.uuid4())
    delivered["correlationid"] = str(uuid.uuid4())
    delivered["causationid"] = None
    delivered["time"] = "2026-08-01T12:00:00Z"
    # idempotency_key and semantic data intentionally remain stable.
    return delivered


def write_contractor_registry(tmp_path, *, project_root=None, version=1):
    root = (project_root or tmp_path).resolve()
    registry = {
        "schema_version": 1,
        "agents": {
            "bloodbank-pm": {
                "profile_name": "bloodbank-pm",
                "project_path": str(root),
                "bloodbank": {
                    "enabled": True,
                    "gateway_scope": "fleet",
                    "target_agent_id": "bloodbank-pm",
                    "contractors": {
                        "board-cranker": {
                            "version": version,
                        }
                    },
                },
            }
        },
    }
    (tmp_path / "missing-registry.yaml").write_text(
        json.dumps(registry), encoding="utf-8"
    )
    return root


@dataclass(frozen=True)
class FakeHermesContractorContext:
    contractor_id: str
    contractor_version: int
    memory_policy: str
    continuity: bool
    required_skills: tuple[str, ...]
    profile_name: str
    project_root: str


@pytest.fixture(autouse=True)
def hermes_contractor_context_type(monkeypatch):
    gateway_base = sys.modules["gateway.platforms.base"]
    monkeypatch.setattr(
        gateway_base,
        "ContractorTurnContext",
        FakeHermesContractorContext,
        raising=False,
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"type": "session"}, "type"),
        ({"contractor_id": ""}, "contractor_id"),
        ({"contractor_version": True}, "contractor_version"),
        ({"contractor_version": 0}, "contractor_version"),
        ({"memory_policy": "profile"}, "memory_policy"),
        ({"continuity": True}, "continuity"),
        ({"required_skills": []}, "required_skills"),
        ({"required_skills": ["alpha", ""]}, "required_skills"),
        ({"required_skills": ["alpha", "alpha"]}, "required_skills"),
        ({"profile": "operations"}, "unsupported"),
        ({"workdir": "/tmp"}, "unsupported"),
        ({"project_root": "/tmp"}, "unsupported"),
        ({"credentials": {"token": "not-a-secret"}}, "unsupported"),
        ({"toolsets": ["terminal"]}, "unsupported"),
        ({"effects": ["deploy"]}, "unsupported"),
        ({"invocation_id": str(uuid.uuid4())}, "unsupported"),
    ],
)
def test_contractor_context_rejects_invalid_or_widening_input(
    valid_command, overrides, message
):
    command = contractor_command(valid_command, **overrides)
    with pytest.raises(CommandInvalid, match=message):
        decode_command(json.dumps(command).encode())


def test_valid_contractor_context_is_preserved_in_order(valid_command):
    command = contractor_command(valid_command)
    decoded = decode_command(json.dumps(command).encode())
    assert decoded["data"]["context"]["required_skills"] == [
        "bloodbank-integration",
        "coding-strategy",
    ]


@pytest.mark.parametrize("field", ["thread_id", "turn_id"])
def test_contractor_context_rejects_caller_lifecycle_identity(valid_command, field):
    command = contractor_command(valid_command)
    command["data"][field] = f"caller-selected-{field}"

    with pytest.raises(CommandInvalid, match=field):
        decode_command(json.dumps(command).encode())


def test_contractor_route_requires_matching_registry_identity_and_owned_root(
    tmp_path,
):
    context_type = getattr(
        __import__("bloodbank_hermes_gateway.contract", fromlist=["ContractorContext"]),
        "ContractorContext",
        None,
    )
    assert context_type is not None, "contract module must expose ContractorContext"
    root = write_contractor_registry(tmp_path)
    resolver = ProfileResolver(
        target_profiles={"bloodbank-pm": "bloodbank-pm"},
        fleet_registry=tmp_path / "missing-registry.yaml",
        allow_direct_profile_targets=True,
        normalize_profile_name=lambda value: value,
        validate_profile_name=lambda _value: None,
        profile_exists=lambda value: value == "bloodbank-pm",
    )
    context = context_type.from_mapping(contractor_context())

    resolved = resolver.resolve_contractor("bloodbank-pm", context)

    assert resolved.profile_name == "bloodbank-pm"
    assert resolved.project_root == str(root)
    assert resolved.contractor_id == "board-cranker"
    assert resolved.contractor_version == 1
    assert resolved.required_skills == (
        "bloodbank-integration",
        "coding-strategy",
    )


@pytest.mark.parametrize(
    ("registry_version", "context_version", "message"),
    [(2, 1, "version"), (1, 2, "version")],
)
def test_contractor_route_rejects_unregistered_version(
    tmp_path, registry_version, context_version, message
):
    contract_module = __import__(
        "bloodbank_hermes_gateway.contract", fromlist=["ContractorContext"]
    )
    context_type = getattr(contract_module, "ContractorContext", None)
    assert context_type is not None, "contract module must expose ContractorContext"
    write_contractor_registry(tmp_path, version=registry_version)
    resolver = ProfileResolver(
        target_profiles={"bloodbank-pm": "bloodbank-pm"},
        fleet_registry=tmp_path / "missing-registry.yaml",
        allow_direct_profile_targets=True,
        normalize_profile_name=lambda value: value,
        validate_profile_name=lambda _value: None,
        profile_exists=lambda _value: True,
    )
    context = context_type.from_mapping(
        contractor_context(contractor_version=context_version)
    )
    with pytest.raises(CommandInvalid, match=message):
        resolver.resolve_contractor("bloodbank-pm", context)


def test_contractor_route_requires_registered_contractor_id(tmp_path):
    write_contractor_registry(tmp_path)
    resolver = ProfileResolver(
        target_profiles={"bloodbank-pm": "bloodbank-pm"},
        fleet_registry=tmp_path / "missing-registry.yaml",
        allow_direct_profile_targets=True,
        normalize_profile_name=lambda value: value,
        validate_profile_name=lambda _value: None,
        profile_exists=lambda _value: True,
    )

    context = ContractorContext.from_mapping(
        contractor_context(contractor_id="unregistered")
    )
    with pytest.raises(CommandInvalid, match="not registered"):
        resolver.resolve_contractor("bloodbank-pm", context)


def test_contractor_route_default_denies_ineligible_registry_target(tmp_path):
    write_contractor_registry(tmp_path)
    registry_path = tmp_path / "missing-registry.yaml"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["agents"]["bloodbank-pm"]["bloodbank"]["enabled"] = False
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    resolver = ProfileResolver(
        target_profiles={"bloodbank-pm": "bloodbank-pm"},
        fleet_registry=registry_path,
        allow_direct_profile_targets=True,
        normalize_profile_name=lambda value: value,
        validate_profile_name=lambda _value: None,
        profile_exists=lambda _value: True,
    )
    context = ContractorContext.from_mapping(contractor_context())

    with pytest.raises(RouteInvalid) as rejected:
        resolver.resolve_contractor("bloodbank-pm", context)

    assert str(rejected.value) == (
        "target_agent_id 'bloodbank-pm' is not eligible for contractor execution"
    )


@pytest.mark.asyncio
async def test_adapter_dispatches_typed_registry_context_to_hermes(
    tmp_path, valid_command
):
    root = write_contractor_registry(tmp_path)
    adapter = make_adapter(tmp_path, target_profiles={"bloodbank-pm": "bloodbank-pm"})
    seen = []

    async def handle(event):
        seen.append(event)
        return "done"

    adapter.set_message_handler(handle)
    message = FakeMessage(contractor_command(valid_command))
    await adapter._handle_broker_message(message)

    assert message.acked == 1
    assert message.termed == message.nacked == 0
    assert len(seen) == 1
    context = seen[0].contractor_context
    assert isinstance(context, FakeHermesContractorContext)
    assert context.profile_name == "bloodbank-pm"
    assert context.project_root == str(root)
    assert context.memory_policy == "none"
    assert context.continuity is False
    assert context.required_skills == (
        "bloodbank-integration",
        "coding-strategy",
    )


@pytest.mark.asyncio
async def test_same_target_and_idempotency_key_executes_once_across_command_ids(
    tmp_path, valid_command
):
    write_contractor_registry(tmp_path)
    adapter = make_adapter(tmp_path, target_profiles={})
    executions = []

    async def handle(event):
        executions.append(event.message_id)
        return "done"

    adapter.set_message_handler(handle)
    original = contractor_command(valid_command)
    first = FakeMessage(original)
    await adapter._handle_broker_message(first)
    first_events = copy.deepcopy(adapter._js.events)

    alias = FakeMessage(redeliver_as_new_command(original))
    await adapter._handle_broker_message(alias)

    assert executions == [original["command_id"]]
    assert first.acked == alias.acked == 1
    assert alias.termed == alias.nacked == 0
    assert adapter._js.events[:4] == first_events
    assert adapter._js.events[4:] == first_events


@pytest.mark.asyncio
async def test_concurrent_duplicate_delivery_runs_only_one_turn(tmp_path, valid_command):
    write_contractor_registry(tmp_path)
    adapter = make_adapter(tmp_path, target_profiles={})
    entered = asyncio.Event()
    release = asyncio.Event()
    executions = []

    async def handle(event):
        executions.append(event.message_id)
        entered.set()
        await release.wait()
        return "done"

    adapter.set_message_handler(handle)
    original = contractor_command(valid_command)
    first = FakeMessage(original)
    alias = FakeMessage(redeliver_as_new_command(original))
    first_task = asyncio.create_task(adapter._handle_broker_message(first))
    await asyncio.wait_for(entered.wait(), timeout=1)
    alias_task = asyncio.create_task(adapter._handle_broker_message(alias))
    # On the unchanged base the alias wrongly enters a second turn and blocks
    # on ``release``.  Releasing after the broker task has had a chance to claim
    # makes that behavior an ordinary assertion failure instead of a hung RED.
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait_for(asyncio.gather(first_task, alias_task), timeout=1)

    assert executions == [original["command_id"]]
    assert alias.nacked == 1
    assert alias.acked == alias.termed == 0
    assert first.acked == 1


@pytest.mark.asyncio
async def test_restart_replays_original_lifecycle_without_reexecution(
    tmp_path, valid_command
):
    write_contractor_registry(tmp_path)
    original = contractor_command(valid_command)
    first_adapter = make_adapter(tmp_path, target_profiles={})
    executions = []

    async def handle(event):
        executions.append(event.message_id)
        return "done"

    first_adapter.set_message_handler(handle)
    await first_adapter._handle_broker_message(FakeMessage(original))
    original_events = copy.deepcopy(first_adapter._js.events)

    restarted = make_adapter(tmp_path, target_profiles={})
    restarted.set_message_handler(handle)
    alias = FakeMessage(redeliver_as_new_command(original))
    await restarted._handle_broker_message(alias)

    assert alias.acked == 1
    assert executions == [original["command_id"]]
    assert restarted._js.events == original_events


@pytest.mark.asyncio
async def test_contractor_restart_finishes_stored_rejection_closure(
    tmp_path, valid_command
):
    from bloodbank_hermes_gateway.contract import Invocation, started_events
    from bloodbank_hermes_gateway.execution_state import (
        envelope_digest,
        execution_semantic_digest,
    )

    write_contractor_registry(tmp_path)
    original = contractor_command(valid_command)
    first = make_adapter(tmp_path, target_profiles={})
    invocation = Invocation.from_envelope(original, "bloodbank-pm")
    digest = envelope_digest(original)
    claimed = first.execution_state.claim_pending(
        command_id=original["command_id"],
        digest=digest,
        semantic_digest=execution_semantic_digest(original),
        target_agent_id=original["data"]["target_agent_id"],
        idempotency_key=original["idempotency_key"],
        profile="bloodbank-pm",
        started_events=started_events(invocation),
    )
    first.execution_state.mark_started(
        command_id=claimed.command_id,
        digest=digest,
    )
    closing = first.execution_state.begin_rejection(
        command_id=claimed.command_id,
        digest=digest,
        reason="route_policy_invalid_before_dispatch",
    )
    assert closing.state == "rejected_closing"

    restarted = make_adapter(tmp_path, target_profiles={})
    executions = []
    restarted.set_message_handler(lambda event: executions.append(event))
    delivery = FakeMessage(redeliver_as_new_command(original))

    await restarted._handle_broker_message(delivery)

    closed = restarted.execution_state.get(original["command_id"])
    assert closed is not None and closed.state == "rejected_closed"
    assert executions == []
    assert delivery.termed == 1
    assert delivery.acked == delivery.nacked == 0
    assert tuple(restarted._js.events) == (
        *closed.started_events,
        *closed.terminal_events,
    )


@pytest.mark.asyncio
async def test_semantic_collision_is_terminal_and_preserves_original_journal(
    tmp_path, valid_command
):
    write_contractor_registry(tmp_path)
    adapter = make_adapter(tmp_path, target_profiles={})
    executions = []

    async def handle(event):
        executions.append(event.message_id)
        return "done"

    adapter.set_message_handler(handle)
    original = contractor_command(valid_command)
    await adapter._handle_broker_message(FakeMessage(original))
    before = adapter.execution_state.get(original["command_id"])
    assert before is not None

    collided = redeliver_as_new_command(original)
    collided["data"]["prompt"] = "Deploy an unrelated system."
    collision = FakeMessage(collided)
    await adapter._handle_broker_message(collision)

    after = adapter.execution_state.get(original["command_id"])
    assert collision.termed == 1
    assert collision.acked == collision.nacked == 0
    assert executions == [original["command_id"]]
    assert after == before
    assert adapter.execution_state.get(collided["command_id"]) is None
    with sqlite3.connect(adapter.execution_state.path) as db:
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_claim_time_command_collision_is_terminal(
    tmp_path, valid_command, monkeypatch
):
    execution_module = __import__(
        "bloodbank_hermes_gateway.execution_state",
        fromlist=["ExecutionEnvelopeCollision"],
    )
    collision_type = getattr(execution_module, "ExecutionEnvelopeCollision", None)
    assert collision_type is not None, "journal must expose a typed envelope collision"
    write_contractor_registry(tmp_path)
    adapter = make_adapter(tmp_path, target_profiles={})

    monkeypatch.setattr(adapter.execution_state, "get", lambda _command_id: None)
    monkeypatch.setattr(
        adapter.execution_state,
        "get_by_identity",
        lambda _target, _idempotency: None,
    )

    def collide(**_kwargs):
        raise collision_type("command identity collision")

    monkeypatch.setattr(adapter.execution_state, "claim_pending", collide)
    message = FakeMessage(contractor_command(valid_command))

    await adapter._handle_broker_message(message)

    assert message.termed == 1
    assert message.acked == message.nacked == 0


def test_command_collision_preserves_existing_journal(tmp_path, valid_command):
    execution_module = __import__(
        "bloodbank_hermes_gateway.execution_state",
        fromlist=["ExecutionEnvelopeCollision"],
    )
    collision_type = getattr(execution_module, "ExecutionEnvelopeCollision", None)
    assert collision_type is not None, "journal must expose a typed envelope collision"
    from bloodbank_hermes_gateway.contract import Invocation, started_events
    from bloodbank_hermes_gateway.execution_state import (
        envelope_digest,
        execution_semantic_digest,
    )

    command = contractor_command(valid_command)
    collided = copy.deepcopy(command)
    collided["data"]["prompt"] = "Different executable intent."
    store = ExecutionStateStore(tmp_path / "command-collision.sqlite3")
    store.claim_pending(
        command_id=command["command_id"],
        digest=envelope_digest(command),
        semantic_digest=execution_semantic_digest(command),
        target_agent_id=command["data"]["target_agent_id"],
        idempotency_key=command["idempotency_key"],
        profile="bloodbank-pm",
        started_events=started_events(Invocation.from_envelope(command, "bloodbank-pm")),
    )
    before = store.get(command["command_id"])

    with pytest.raises(collision_type):
        store.claim_pending(
            command_id=collided["command_id"],
            digest=envelope_digest(collided),
            semantic_digest=execution_semantic_digest(collided),
            target_agent_id=collided["data"]["target_agent_id"],
            idempotency_key=collided["idempotency_key"],
            profile="bloodbank-pm",
            started_events=started_events(
                Invocation.from_envelope(collided, "bloodbank-pm")
            ),
        )

    assert store.get(command["command_id"]) == before


def test_journal_claim_is_race_safe_for_target_scoped_identity(tmp_path, valid_command):
    execution_module = __import__(
        "bloodbank_hermes_gateway.execution_state",
        fromlist=["execution_semantic_digest"],
    )
    semantic_digest = getattr(execution_module, "execution_semantic_digest", None)
    assert semantic_digest is not None, "journal must expose semantic digest"
    command = contractor_command(valid_command)
    alias = redeliver_as_new_command(command)
    store = ExecutionStateStore(tmp_path / "race.sqlite3")

    from bloodbank_hermes_gateway.contract import Invocation, started_events
    from bloodbank_hermes_gateway.execution_state import envelope_digest

    invocation = Invocation.from_envelope(command, "bloodbank-pm")
    alias_invocation = Invocation.from_envelope(alias, "bloodbank-pm")
    barrier = threading.Barrier(2)

    def claim(envelope, pending_invocation):
        barrier.wait(timeout=2)
        return store.claim_pending(
            command_id=envelope["command_id"],
            digest=envelope_digest(envelope),
            semantic_digest=semantic_digest(envelope),
            target_agent_id="bloodbank-pm",
            idempotency_key=envelope["idempotency_key"],
            profile="bloodbank-pm",
            started_events=started_events(pending_invocation),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(claim, command, invocation),
            executor.submit(claim, alias, alias_invocation),
        ]
        first, second = (future.result(timeout=3) for future in futures)

    assert first.command_id == second.command_id
    assert first.command_id in {command["command_id"], alias["command_id"]}
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 1


def test_journal_idempotency_identity_is_target_scoped(tmp_path, valid_command):
    from bloodbank_hermes_gateway.contract import Invocation, started_events
    from bloodbank_hermes_gateway.execution_state import (
        envelope_digest,
        execution_semantic_digest,
    )

    first_command = contractor_command(valid_command)
    second_command = copy.deepcopy(first_command)
    second_command["id"] = str(uuid.uuid4())
    second_command["command_id"] = str(uuid.uuid4())
    second_command["data"]["target_agent_id"] = "other-pm"
    store = ExecutionStateStore(tmp_path / "target-scope.sqlite3")

    records = []
    for envelope, target in (
        (first_command, "bloodbank-pm"),
        (second_command, "other-pm"),
    ):
        invocation = Invocation.from_envelope(envelope, target)
        records.append(
            store.claim_pending(
                command_id=envelope["command_id"],
                digest=envelope_digest(envelope),
                semantic_digest=execution_semantic_digest(envelope),
                target_agent_id=target,
                idempotency_key=envelope["idempotency_key"],
                profile=target,
                started_events=started_events(invocation),
            )
        )

    assert records[0].command_id != records[1].command_id
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 2
