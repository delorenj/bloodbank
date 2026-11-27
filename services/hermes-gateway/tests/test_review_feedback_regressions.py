"""Accepted BB-67 review counterexamples, promoted as disposable regressions."""
import asyncio
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_review_feedback import adapter, frozen
from test_adapter import FakeMessage
from test_final_answer import capture
from core.review_feedback import build_command, evidence_state, invocation_data
from core.validate import validate_envelope
from bloodbank_hermes_gateway.contract import (
    Invocation, final_answer_event, queued_event, started_events, terminal_events,
)
from bloodbank_hermes_gateway.execution_state import envelope_digest


def registry(gateway, *, enabled=True, present=True, scope="fleet", profile="green-in-between-pm"):
    gateway.profile_resolver.fleet_registry.write_text(json.dumps({
        "schema_version": 1, "agents": {"green-in-between-pm": {
            "profile_name": profile, "bloodbank": {"enabled": enabled,
                "gateway_scope": scope, "target_agent_id": "green-in-between-pm"},
        }} if present else {},
    }))


@pytest.mark.parametrize("phase", ["queued", "started", "failed", "answer"])
@pytest.mark.parametrize("change", ["lane", "causationid", "id", "ordering_key", "command_type", "command_digest", "command_event_id", "command_causationid", "issuer"])
def test_exact_feedback_lineage_required(phase, change):
    command = build_command(frozen())
    inv = Invocation.from_envelope(command, "green-in-between-pm", "feedback")
    event = {
        "queued": lambda: queued_event(inv),
        "started": lambda: started_events(inv)[1],
        "failed": lambda: terminal_events(inv, outcome="failure")[0],
        "answer": lambda: final_answer_event(inv, role="assistant", final_answer=True,
            text="Disposable native answer", native_session_id="observed-session", native_turn_id="observed-turn"),
    }[phase]()
    assert evidence_state(command, event, profile=inv.profile) is not None
    if change in {"command_event_id", "command_causationid", "issuer"}:
        event["data"][change] = ({"type": "operator", "agent_id": "other-reviewer"}
            if change == "issuer" else "00000000-0000-4000-8000-000000000099")
    elif change in {"lane", "command_type", "command_digest"}:
        event["data"][change] = {"lane": "batch", "command_type": "bloodbank.agent.invocation.start", "command_digest": "a" * 64}[change]
    else:
        event[change] = "turn:unrelated" if change == "ordering_key" else "00000000-0000-4000-8000-000000000099"
    validate_envelope(event)
    assert evidence_state(command, event, profile=inv.profile) is None


@pytest.mark.asyncio
async def test_genuine_batch_answer_cannot_answer_unexecuted_feedback(tmp_path, monkeypatch):
    command = build_command(frozen())
    batch = copy.deepcopy(command)
    batch.update(type="bloodbank.agent.invocation.start", domain="agent",
        subject="bloodbank.cmd.agent.invocation.start",
        dataschema="apicurio://holyfields/bloodbank.agent.invocation.start/versions/1",
        schemaref="bloodbank.agent.invocation.start.v1")
    batch["data"] = invocation_data(command)
    batch["data"]["prompt"] = "A different disposable task"
    gateway = adapter(tmp_path, monkeypatch)
    async def handler(event):
        assert event.text == batch["data"]["prompt"]
        capture("Answer to another task")
        return "local"
    gateway.set_message_handler(handler)
    first = FakeMessage(batch)
    await gateway._handle_broker_message(first, gateway.lanes[0])
    answer = next(e for e in gateway._js.events if e["type"] == "bloodbank.conversation.message.appended")
    assert first.acked == 1 and answer["data"]["lane"] == "batch"
    assert evidence_state(command, answer, profile="green-in-between-pm") is None
    second = FakeMessage(command)
    await gateway._handle_broker_message(second, gateway.lanes[-1])
    assert second.termed == 1
    # Even changing the lane alone cannot impersonate executed feedback.
    answer["data"]["lane"] = "feedback"
    assert evidence_state(command, answer, profile="green-in-between-pm") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled_override", "missing_direct", "scope", "invalid_enabled", "wrong_profile"])
async def test_feedback_eligibility_independent_of_legacy_overrides(tmp_path, monkeypatch, mode):
    gateway = adapter(tmp_path, monkeypatch)
    registry(gateway, enabled=False if mode == "disabled_override" else "true" if mode == "invalid_enabled" else True,
        present=mode != "missing_direct", scope="local" if mode == "scope" else "fleet",
        profile="bloodbank-pm" if mode == "wrong_profile" else "green-in-between-pm")
    if mode == "missing_direct":
        gateway.profile_resolver.target_profiles.clear()
        gateway.profile_resolver.allow_direct_profile_targets = True
    calls = []
    async def forbidden(event): calls.append(event)
    gateway.set_message_handler(forbidden)
    command = build_command(frozen())
    msg = FakeMessage(command)
    await gateway._handle_broker_message(msg, gateway.lanes[-1])
    assert not calls and msg.termed == 1
    assert gateway.execution_state.get(command["command_id"]).state == "rejected_pre_start"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["queued", "started"])
async def test_disable_during_publication_refuses_before_dispatch(tmp_path, monkeypatch, phase):
    gateway = adapter(tmp_path, monkeypatch)
    publish = gateway._js.publish
    async def disable(subject, payload, **kwargs):
        if json.loads(payload)["type"] == f"bloodbank.agent.invocation.{phase}":
            registry(gateway, enabled=False)
        await publish(subject, payload, **kwargs)
    gateway._js.publish = disable
    async def forbidden(_): raise AssertionError("must not dispatch after registry disable")
    gateway.set_message_handler(forbidden)
    msg = FakeMessage(build_command(frozen()))
    await gateway._handle_broker_message(msg, gateway.lanes[-1])
    assert msg.termed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["registry", "registry_after_queue", "registry_after_start", "queued_puback"])
async def test_last_delivery_closes_and_outbox_recovers_without_redelivery(tmp_path, monkeypatch, fault):
    command = build_command(frozen())
    first = adapter(tmp_path, monkeypatch)
    original = first._js.publish
    if fault == "registry":
        first.profile_resolver.fleet_registry.unlink()
    async def publish(subject, payload, **kwargs):
        if fault == "queued_puback": raise RuntimeError("disposable publication outage")
        await original(subject, payload, **kwargs)
        phase = json.loads(payload)["type"]
        if (fault == "registry_after_queue" and phase == "bloodbank.agent.invocation.queued") or (
            fault == "registry_after_start" and phase == "bloodbank.agent.invocation.started"
        ):
            first.profile_resolver.fleet_registry.unlink(missing_ok=True)
    first._js.publish = publish
    async def forbidden(_): raise AssertionError("exhausted command must never dispatch")
    first.set_message_handler(forbidden)
    msg = FakeMessage(command)
    setattr(msg, "metadata", SimpleNamespace(num_delivered=first.lanes[-1].max_deliver))
    await first._handle_broker_message(msg, first.lanes[-1])
    row = first.execution_state.get(command["command_id"])
    assert row is not None
    assert row.state == "completed" and row.failure_code == "feedback_delivery_exhausted"
    assert bool(row.started_events) == (fault == "registry_after_start")
    assert len(row.terminal_events) == (2 if fault == "registry_after_start" else 1)
    assert evidence_state(command, row.terminal_events[0], profile="green-in-between-pm") == "uncertain"
    if fault == "queued_puback":
        assert row.outbox_published_at is None
        second = adapter(tmp_path, monkeypatch)
        # Real fetch cycle drains the journal before consulting an empty broker.
        class EmptySubscription:
            async def fetch(self, *args, **kwargs): raise TimeoutError
        lane = second.lanes[-1]
        lane.subscription = EmptySubscription()
        await second._consume_once(lane)
        recovered = second.execution_state.get(command["command_id"])
        assert recovered.outbox_published_at is not None
        assert tuple(second._js.events) == row.queued_events + row.terminal_events
        await second._consume_once(lane)
        assert len(second._js.events) == 2
        second.set_message_handler(forbidden)
        replay = FakeMessage(command)
        await second._handle_broker_message(replay, lane)
        assert replay.acked == 1


@pytest.mark.asyncio
async def test_two_same_process_adapters_cannot_dispatch_or_close_live_owner(tmp_path, monkeypatch):
    command = build_command(frozen())
    a, b = adapter(tmp_path, monkeypatch), adapter(tmp_path, monkeypatch)
    assert a.lanes[-1].durable == b.lanes[-1].durable
    queued, continue_queue, running, finish = (asyncio.Event() for _ in range(4))
    original = a._js.publish
    async def gate(subject, payload, **kwargs):
        if json.loads(payload)["type"] == "bloodbank.agent.invocation.queued":
            queued.set()
            await continue_queue.wait()
        await original(subject, payload, **kwargs)
    a._js.publish = gate
    calls = []
    async def handler(event):
        calls.append(event.message_id)
        running.set()
        await finish.wait()
        capture()
        return "local"
    a.set_message_handler(handler)
    b.set_message_handler(handler)
    first = FakeMessage(command)
    task = asyncio.create_task(a._handle_broker_message(first, a.lanes[-1]))
    try:
        await asyncio.wait_for(queued.wait(), 5)
        pending_duplicate = FakeMessage(command)
        await b._handle_broker_message(pending_duplicate, b.lanes[-1])
        assert pending_duplicate.nacked == 1 and not calls
        assert b.execution_state.get(command["command_id"]).state == "pending"
        continue_queue.set()
        await asyncio.wait_for(running.wait(), 5)
        started_duplicate = FakeMessage(command)
        await b._handle_broker_message(started_duplicate, b.lanes[-1])
        assert started_duplicate.nacked == 1 and len(calls) == 1
        assert b.execution_state.get(command["command_id"]).state == "started"
    finally:
        continue_queue.set(); finish.set()
        await asyncio.wait_for(task, 5)
    assert first.acked == 1
    replay = FakeMessage(command)
    await b._handle_broker_message(replay, b.lanes[-1])
    assert replay.acked == 1 and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True])
async def test_kernel_release_after_real_process_exit_allows_safe_recovery(tmp_path, monkeypatch, started):
    command = build_command(frozen())
    gateway = adapter(tmp_path, monkeypatch)
    payload = tmp_path / "command.json"
    payload.write_text(json.dumps(command))
    script = """
import json, sys, importlib.util, types
from pathlib import Path
root=Path(sys.argv[2]).parent
source=Path(sys.argv[4])
sys.path.insert(0,str(source.parents[1]/'agent-hooks'))
package=types.ModuleType('bloodbank_hermes_gateway'); package.__path__=[str(source)]
sys.modules['bloodbank_hermes_gateway']=package
from bloodbank_hermes_gateway.execution_state import ExecutionStateStore, envelope_digest
from bloodbank_hermes_gateway.contract import Invocation, queued_event, started_events
s=ExecutionStateStore(Path(sys.argv[1])); e=json.loads(Path(sys.argv[2]).read_text())
i=Invocation.from_envelope(e, 'green-in-between-pm', 'feedback'); d=envelope_digest(e)
fd=s.try_feedback_lock(e['command_id']); assert fd is not None
s.claim_pending(command_id=e['command_id'],digest=d,profile=i.profile,queued_events=(queued_event(i),),lane='feedback')
if sys.argv[3]=='True': s.mark_started(command_id=e['command_id'],digest=d,started_events=started_events(i))
print('owned',flush=True)
sys.stdin.read()
"""
    source = Path(__file__).resolve().parents[1] / "bloodbank_hermes_gateway"
    child = subprocess.Popen([sys.executable, "-c", script, str(gateway.execution_state.path), str(payload), str(started), str(source)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    assert child.stdout is not None and child.stderr is not None
    try:
        line = await asyncio.to_thread(child.stdout.readline)
        assert line == "owned\n", child.stderr.read() if not line else line
        assert gateway.execution_state.try_feedback_lock(command["command_id"]) is None
        await gateway._reconcile_feedback_result_outbox()
        alive = gateway.execution_state.get(command["command_id"])
        assert alive is not None and alive.state == ("started" if started else "pending")
        assert not gateway._js.events
        child.kill()
        assert await asyncio.to_thread(child.wait, 5) != 0
    finally:
        if child.poll() is None: child.kill(); child.wait()
        child.communicate()
    if started:
        await gateway._reconcile_feedback_result_outbox()
        after_death = gateway.execution_state.get(command["command_id"])
        assert after_death is not None and after_death.failure_code == "feedback_execution_uncertain"
        assert after_death.outbox_published_at is not None
    calls = []
    async def handler(event): calls.append(event); capture(); return "local"
    gateway.set_message_handler(handler)
    msg = FakeMessage(command)
    await gateway._handle_broker_message(msg, gateway.lanes[-1])
    assert msg.acked == 1
    assert len(calls) == (0 if started else 1)
    if started:
        assert gateway.execution_state.get(command["command_id"]).failure_code == "feedback_execution_uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure"])
async def test_completed_feedback_outbox_recovers_without_command_delivery(tmp_path, monkeypatch, outcome):
    command = build_command(frozen())
    gateway = adapter(tmp_path, monkeypatch)
    inv = Invocation.from_envelope(command, "green-in-between-pm", "feedback")
    digest = envelope_digest(command)
    gateway.execution_state.claim_pending(command_id=command["command_id"], digest=digest,
        profile=inv.profile, lane="feedback", queued_events=(queued_event(inv),))
    gateway.execution_state.mark_started(command_id=command["command_id"], digest=digest,
        started_events=started_events(inv))
    events = terminal_events(inv, outcome=outcome)
    if outcome == "success":
        events += (final_answer_event(inv, role="assistant", final_answer=True,
            text="Stored native final", native_session_id="observed-session", native_turn_id="observed-turn"),)
    original = gateway.execution_state.mark_completed(command_id=command["command_id"],
        digest=digest, outcome=outcome, terminal_events=events)
    async def outage(*_args, **_kwargs): raise RuntimeError("disposable PubAck outage")
    gateway._js.publish = outage
    await gateway._reconcile_feedback_result_outbox()
    unpublished = gateway.execution_state.get(command["command_id"])
    assert unpublished is not None and unpublished.outbox_published_at is None
    recovered = adapter(tmp_path, monkeypatch)
    await recovered._reconcile_feedback_result_outbox()
    expected = original.queued_events + original.started_events + original.terminal_events
    assert tuple(recovered._js.events) == expected
    await recovered._reconcile_feedback_result_outbox()
    assert tuple(recovered._js.events) == expected
    result = recovered.execution_state.get(command["command_id"])
    assert result is not None and result.outcome == outcome and result.terminal_events == original.terminal_events


def test_failed_phase_causation_is_not_interchangeable():
    command = build_command(frozen())
    inv = Invocation.from_envelope(command, "green-in-between-pm", "feedback")
    failure = terminal_events(inv, outcome="failure")[0]
    failure["causationid"] = queued_event(inv)["id"]
    assert evidence_state(command, failure, profile=inv.profile) is None


def test_exhausted_pending_result_is_byte_immutable(tmp_path, monkeypatch):
    command = build_command(frozen())
    gateway = adapter(tmp_path, monkeypatch)
    inv = Invocation.from_envelope(command, "green-in-between-pm", "feedback")
    digest = envelope_digest(command)
    gateway.execution_state.claim_pending(command_id=command["command_id"], digest=digest,
        profile=inv.profile, lane="feedback", queued_events=(queued_event(inv),))
    event = terminal_events(inv, outcome="failure")[0]
    event["data"]["error_code"] = "feedback_delivery_exhausted"
    gateway.execution_state.close_pending_feedback(command_id=command["command_id"],
        digest=digest, terminal_events=(event,))
    assert gateway.execution_state.close_pending_feedback(command_id=command["command_id"],
        digest=digest, terminal_events=(event,)).terminal_events == (event,)
    changed = copy.deepcopy(event)
    changed["data"]["error_message"] = "different bytes"
    with pytest.raises(RuntimeError, match="immutable"):
        gateway.execution_state.close_pending_feedback(command_id=command["command_id"],
            digest=digest, terminal_events=(changed,))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["queued", "turn_started", "invocation_started", "pre_start_refusal", "post_start_refusal", "turn_closure"])
@pytest.mark.parametrize("puback_lost", [False, True])
async def test_phase_wide_final_delivery_publication_recovery(tmp_path, monkeypatch, phase, puback_lost):
    """Promoted rereview faults, plus acceptance-before-lost-receipt boundaries."""
    command = build_command(frozen())
    first = adapter(tmp_path, monkeypatch)
    if phase == "pre_start_refusal":
        registry(first, enabled=False)
    original = first._js.publish
    target = {
        "queued": "bloodbank.agent.invocation.queued",
        "turn_started": "bloodbank.conversation.turn.started",
        "invocation_started": "bloodbank.agent.invocation.started",
        "pre_start_refusal": "bloodbank.agent.invocation.failed",
        "post_start_refusal": "bloodbank.agent.invocation.failed",
        "turn_closure": "bloodbank.conversation.turn.completed",
    }[phase]
    async def fault(subject, payload, **kwargs):
        kind = json.loads(payload)["type"]
        if phase in {"post_start_refusal", "turn_closure"} and kind == "bloodbank.agent.invocation.started":
            registry(first, enabled=False)
        if kind == target:
            if puback_lost:
                await original(subject, payload, **kwargs)
            raise RuntimeError("disposable terminal delivery PubAck fault")
        await original(subject, payload, **kwargs)
    first._js.publish = fault
    calls = []
    async def forbidden(event):
        calls.append(event)
        raise AssertionError("publication/refusal must precede dispatch")
    first.set_message_handler(forbidden)
    msg = FakeMessage(command)
    setattr(msg, "metadata", SimpleNamespace(num_delivered=first.lanes[-1].max_deliver))
    await first._handle_broker_message(msg, first.lanes[-1])
    row = first.execution_state.get(command["command_id"])
    assert row is not None and row.feedback_exhausted_at is not None
    assert msg.nacked == 1 and not msg.acked and not msg.termed and not calls
    stored = row.terminal_events or row.feedback_closure_events
    assert stored
    second = adapter(tmp_path, monkeypatch)
    second.set_message_handler(forbidden)
    class Empty:
        async def fetch(self, *args, **kwargs): raise TimeoutError
    second.lanes[-1].subscription = Empty()
    await second._consume_once(second.lanes[-1])
    after = second.execution_state.get(command["command_id"])
    assert after is not None and after.outbox_published_at is not None
    assert tuple(second._js.events) == row.queued_events + row.started_events + stored
    expected = "failed" if "refusal" in phase or phase == "turn_closure" else "uncertain"
    assert evidence_state(command, stored[0], profile=row.profile) == expected
    assert bool(row.started_events) == (phase not in {"queued", "pre_start_refusal"})
    assert (after.terminal_events or after.feedback_closure_events) == stored
    await second._consume_once(second.lanes[-1])
    assert len(second._js.events) == len(row.queued_events + row.started_events + stored)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["pending", "pending_exhausted", "started", "captured", "rejected_pre_start", "rejected_closing", "rejected_closed"])
async def test_startup_recovers_each_abandoned_phase_without_command(tmp_path, monkeypatch, phase):
    """Simulate death between journal transactions, not an exception catch."""
    command = build_command(frozen())
    first = adapter(tmp_path, monkeypatch)
    inv = Invocation.from_envelope(command, "green-in-between-pm", "feedback")
    digest = envelope_digest(command)
    store = first.execution_state
    store.claim_pending(command_id=command["command_id"], digest=digest,
        profile=inv.profile, lane="feedback", queued_events=(queued_event(inv),),
        feedback_exhausted=phase == "pending_exhausted")
    if phase not in {"pending", "pending_exhausted", "rejected_pre_start"}:
        store.mark_started(command_id=command["command_id"], digest=digest, started_events=started_events(inv))
    if phase == "captured":
        store.mark_captured(command_id=command["command_id"], digest=digest, outcome="success",
            events=(final_answer_event(inv, role="assistant", final_answer=True,
                text="Private captured-only final", native_session_id="observed-session", native_turn_id="observed-turn"),))
    if phase == "rejected_pre_start":
        store.reject_pre_start(command_id=command["command_id"], digest=digest, reason="dispatch_route_invalid")
    if phase in {"rejected_closing", "rejected_closed"}:
        rejected = store.begin_rejection(command_id=command["command_id"], digest=digest, reason="dispatch_route_invalid")
        if phase == "rejected_closed":
            store.finalize_rejection(command_id=command["command_id"], digest=digest,
                terminal_events=terminal_events(inv, outcome="failure", failure_code="dispatch_route_invalid",
                    occurred_at=rejected.rejected_at))
    before = store.get(command["command_id"])
    assert before is not None
    # Independent live descriptor prevents ALL phase recovery, not just dispatch.
    fd = store.try_feedback_lock(command["command_id"])
    assert fd is not None
    second = adapter(tmp_path, monkeypatch)
    async def forbidden(_): raise AssertionError("startup must never dispatch")
    second.set_message_handler(forbidden)
    try:
        await second._reconcile_feedback_result_outbox()
        assert second.execution_state.get(command["command_id"]) == before
        assert not second._js.events
    finally:
        os.close(fd)
    # Fresh process view and empty fetch, no manual command redelivery.
    recovered = adapter(tmp_path, monkeypatch)
    class Empty:
        async def fetch(self, *args, **kwargs): raise TimeoutError
    recovered.lanes[-1].subscription = Empty()
    recovered.set_message_handler(forbidden)
    await recovered._consume_once(recovered.lanes[-1])
    after = recovered.execution_state.get(command["command_id"])
    assert after is not None
    if phase == "pending":
        assert after == before and not recovered._js.events
        return  # No start/exhaustion evidence: leave dispatchable, never invent failure.
    assert after.outbox_published_at is not None
    assert after.started_events == before.started_events
    assert after.queued_events == before.queued_events
    events = after.terminal_events or after.feedback_closure_events
    assert tuple(recovered._js.events) == before.queued_events + before.started_events + events
    assert evidence_state(command, events[0], profile=inv.profile) == ("failed" if phase.startswith("rejected") else "uncertain")
    assert not any(e["type"] == "bloodbank.conversation.message.appended" for e in recovered._js.events)
    if phase == "captured":
        assert after.failure_code == "processing_outcome_unknown"
        assert after.captured_events == before.captured_events
    if before.terminal_events:
        assert after.terminal_events == before.terminal_events
    await recovered._consume_once(recovered.lanes[-1])
    assert tuple(recovered._js.events) == before.queued_events + before.started_events + events


@pytest.mark.asyncio
async def test_recovery_retries_immutable_refusal_batch_after_partial_puback(tmp_path, monkeypatch):
    command = build_command(frozen())
    gateway = adapter(tmp_path, monkeypatch)
    registry(gateway, enabled=False)
    original = gateway._js.publish
    async def outage(subject, payload, **kwargs):
        if json.loads(payload)["type"] == "bloodbank.agent.invocation.failed":
            raise RuntimeError("disposable closure outage")
        await original(subject, payload, **kwargs)
    gateway._js.publish = outage
    await gateway._handle_broker_message(FakeMessage(command), gateway.lanes[-1])
    row = gateway.execution_state.get(command["command_id"])
    assert row is not None and row.feedback_closure_events
    changed = copy.deepcopy(row.feedback_closure_events[0])
    changed["data"]["context"]["annotations"] = True
    with pytest.raises(RuntimeError, match="immutable"):
        gateway.execution_state.store_feedback_closure(command_id=command["command_id"],
            digest=envelope_digest(command), events=(changed,))
    await gateway._reconcile_feedback_result_outbox()
    unchanged = gateway.execution_state.get(command["command_id"])
    assert unchanged is not None and unchanged.feedback_closure_events == row.feedback_closure_events
    gateway._js.publish = original
    # Cursor wraps after a failed page; a retry does not replace stored bytes.
    for _ in range(2):
        await gateway._reconcile_feedback_result_outbox()
    after = gateway.execution_state.get(command["command_id"])
    assert after is not None and after.outbox_published_at
    published = [e for e in gateway._js.events if e["type"] == "bloodbank.agent.invocation.failed"]
    assert published == list(row.feedback_closure_events)


@pytest.mark.asyncio
async def test_recovery_cursor_does_not_starve_rows_behind_live_owners(tmp_path, monkeypatch):
    gateway = adapter(tmp_path, monkeypatch)
    locks = []
    commands = []
    try:
        for index in range(101):
            row = frozen()
            row["submissionId"] = f"00000000-0000-4000-8000-{index:012d}"
            command = build_command(row)
            inv = Invocation.from_envelope(command, "green-in-between-pm", "feedback")
            gateway.execution_state.claim_pending(command_id=command["command_id"], digest=envelope_digest(command),
                profile=inv.profile, lane="feedback", queued_events=(queued_event(inv),), feedback_exhausted=True)
            commands.append(command)
        commands.sort(key=lambda e: e["command_id"])
        for command in commands[:100]:
            fd = gateway.execution_state.try_feedback_lock(command["command_id"])
            assert fd is not None
            locks.append(fd)
        await gateway._reconcile_feedback_result_outbox()
        assert not gateway._js.events
        await gateway._reconcile_feedback_result_outbox()
        last = gateway.execution_state.get(commands[-1]["command_id"])
        assert last is not None and last.outbox_published_at is not None
        assert len(gateway._js.events) == 2
    finally:
        for fd in locks:
            os.close(fd)


@pytest.mark.asyncio
async def test_recovery_continues_while_turn_admission_is_draining(tmp_path, monkeypatch):
    command = build_command(frozen())
    gateway = adapter(tmp_path, monkeypatch)
    inv = Invocation.from_envelope(command, "green-in-between-pm", "feedback")
    digest = envelope_digest(command)
    gateway.execution_state.claim_pending(command_id=command["command_id"], digest=digest,
        profile=inv.profile, lane="feedback", queued_events=(queued_event(inv),), feedback_exhausted=True)
    monkeypatch.setattr(gateway, "_gateway_not_accepting", lambda: "external_drain")
    gateway.drain_poll_seconds = 0
    class NoFetch:
        async def fetch(self, *args, **kwargs): raise AssertionError("must not admit commands")
    gateway.lanes[-1].subscription = NoFetch()
    await gateway._consume_once(gateway.lanes[-1])
    result = gateway.execution_state.get(command["command_id"])
    assert result is not None and result.outbox_published_at is not None
    assert evidence_state(command, result.terminal_events[0], profile=inv.profile) == "uncertain"


@pytest.mark.asyncio
async def test_recovery_of_started_row_without_queued_uses_invocation_lineage(tmp_path, monkeypatch):
    command = build_command(frozen())
    gateway = adapter(tmp_path, monkeypatch)
    inv = Invocation.from_envelope(command, "green-in-between-pm", "feedback")
    digest = envelope_digest(command)
    gateway.execution_state.claim_pending(command_id=command["command_id"], digest=digest,
        profile=inv.profile, lane="feedback")
    gateway.execution_state.mark_started(command_id=command["command_id"], digest=digest,
        started_events=started_events(inv))
    await gateway._reconcile_feedback_result_outbox()
    result = gateway.execution_state.get(command["command_id"])
    assert result is not None and result.outbox_published_at is not None
    assert evidence_state(command, result.terminal_events[0], profile=inv.profile) == "uncertain"
