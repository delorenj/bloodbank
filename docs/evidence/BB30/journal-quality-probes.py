"""Independent quality probes for the BB-30 production journal and adapter."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import conftest
import pytest
from conftest import FixtureRunner, valid_command
from test_adapter import FakeMessage, make_adapter
from test_final_answer import BODY, capture, wire
from test_hermes_seam import native, release
from test_native_outcome import native_finish
from bloodbank_hermes_gateway import adapter as adapter_module, contract
from bloodbank_hermes_gateway.contract import Invocation, final_answer_event, started_events, terminal_events
from bloodbank_hermes_gateway.execution_state import ExecutionStateStore, envelope_digest
from bloodbank_hermes_gateway import plugin

ROOT = Path('/home/delorenj/code/33GOD/bloodbank/.worktrees/feat-bb-30-final-assistant-publication')

def test_journal_rejects_semantically_equal_but_byte_distinct_capture(tmp_path, valid_command):
    path = tmp_path / 'byte-equality.sqlite3'
    store, invocation, digest = seed(path, valid_command)
    original = answer(invocation)
    original['data']['quality_metadata'] = {'truth': True}
    stored = store.mark_captured(command_id=invocation.invocation_id, digest=digest,
                                 outcome='success', events=(original,))
    conflicting = copy.deepcopy(original)
    conflicting['data']['quality_metadata']['truth'] = 1
    assert wire(original) != wire(conflicting)
    from bloodbank_hermes_gateway.contract import validate_fact
    validate_fact(original)
    validate_fact(conflicting)
    print(json.dumps({'probe':'schema-valid-capture-byte-conflict', 'different_wire_bytes':True, 'both_schema_valid':True}))
    with pytest.raises(RuntimeError, match='immutable'):
        store.mark_captured(command_id=invocation.invocation_id, digest=digest,
                            outcome='success', events=(conflicting,))
    assert ExecutionStateStore(path).get(invocation.invocation_id) == stored


def test_journal_rejects_semantically_equal_but_byte_distinct_resolution(tmp_path, valid_command):
    path = tmp_path / 'resolved-byte-equality.sqlite3'
    store, invocation, digest = seed(path, valid_command)
    events = terminal_events(invocation, outcome='failure', failure_code='example')
    events[0]['data']['quality_metadata'] = {'truth': True}
    stored = store.mark_completed(command_id=invocation.invocation_id, digest=digest,
                                   outcome='failure', terminal_events=events,
                                   failure_code='example')
    conflicting = copy.deepcopy(events)
    conflicting[0]['data']['quality_metadata']['truth'] = 1
    assert wire(events[0]) != wire(conflicting[0])
    from bloodbank_hermes_gateway.contract import validate_fact
    for event in (*events, *conflicting):
        validate_fact(event)
    print(json.dumps({'probe':'schema-valid-resolution-byte-conflict', 'different_wire_bytes':True, 'both_schema_valid':True}))
    with pytest.raises(RuntimeError, match='immutable'):
        store.mark_completed(command_id=invocation.invocation_id, digest=digest,
                             outcome='failure', terminal_events=conflicting,
                             failure_code='example')
    assert ExecutionStateStore(path).get(invocation.invocation_id) == stored


def seed(path, command):
    store = ExecutionStateStore(path)
    invocation = Invocation.from_envelope(command, 'bloodbank-pm')
    digest = envelope_digest(command)
    store.claim_pending(command_id=invocation.invocation_id, digest=digest,
                        profile=invocation.profile, started_events=started_events(invocation))
    store.mark_started(command_id=invocation.invocation_id, digest=digest)
    return store, invocation, digest


def answer(invocation, text=BODY):
    return final_answer_event(invocation, role='assistant', final_answer=True, text=text,
                             native_session_id='quality-session', native_turn_id='quality-turn')


def test_exact_source_and_schema_identity():
    import sys
    sys.path.insert(0, str(ROOT / 'services/agent-hooks'))
    assert Path(adapter_module.__file__).resolve() == ROOT / 'services/hermes-gateway/bloodbank_hermes_gateway/adapter.py'
    from core import validate
    assert Path(validate.__file__).resolve() == ROOT / 'services/agent-hooks/core/validate.py'
    assert validate._schemas_root().resolve() == ROOT / 'schemas'
    print(json.dumps({'probe':'identity', 'adapter':adapter_module.__file__,
                      'validator':validate.__file__, 'schemas':str(validate._schemas_root())}))


@pytest.mark.parametrize('kind', ['capture', 'resolution'])
def test_real_sqlite_transaction_abort_rolls_back_whole_update(tmp_path, valid_command, kind):
    path = tmp_path / 'atomic.sqlite3'
    store, invocation, digest = seed(path, valid_command)
    if kind == 'resolution':
        store.mark_captured(command_id=invocation.invocation_id, digest=digest,
                            outcome='success', events=(answer(invocation),))
    before = store.get(invocation.invocation_id)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER refuse_update BEFORE UPDATE ON executions BEGIN SELECT RAISE(ABORT, 'quality injected SQLite write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='quality injected'):
        if kind == 'capture':
            store.mark_captured(command_id=invocation.invocation_id, digest=digest,
                                outcome='success', events=(answer(invocation),))
        else:
            store.mark_completed(command_id=invocation.invocation_id, digest=digest,
                                 outcome='success', terminal_events=(*before.captured_events, *terminal_events(invocation, outcome='success')))
    reopened = ExecutionStateStore(path)
    assert reopened.get(invocation.invocation_id) == before
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        db.execute('DROP TRIGGER refuse_update')
    if kind == 'capture':
        # A truly failed capture remains dispatchable, not a fabricated no-rerun boundary.
        assert reopened.mark_started(command_id=invocation.invocation_id, digest=digest).capture_outcome is None
    else:
        with pytest.raises(RuntimeError, match='cannot be dispatched again'):
            reopened.mark_started(command_id=invocation.invocation_id, digest=digest)
    print(json.dumps({'probe':'sqlite-abort-atomicity', 'stage':kind, 'unchanged':True, 'integrity':'ok'}))


@pytest.mark.parametrize('kind', ['capture', 'resolution'])
def test_concurrent_conflicting_journal_writers_first_commit_is_immutable(tmp_path, valid_command, kind):
    path = tmp_path / 'concurrent.sqlite3'
    store, invocation, digest = seed(path, valid_command)
    if kind == 'resolution':
        store.mark_captured(command_id=invocation.invocation_id, digest=digest,
                            outcome='success', events=(answer(invocation),))
    # Use independently opened production stores, not a fake shared lock.
    writers = [ExecutionStateStore(path), ExecutionStateStore(path)]
    gate = threading.Barrier(2)
    events = [(answer(invocation, '  first é\n'),), (answer(invocation, '  second 🐦\n'),)] if kind == 'capture' else [
        terminal_events(invocation, outcome='failure', failure_code='writer_one'),
        terminal_events(invocation, outcome='failure', failure_code='writer_two')]
    def attempt(index):
        gate.wait(timeout=3)
        try:
            if kind == 'capture':
                result = writers[index].mark_captured(command_id=invocation.invocation_id, digest=digest,
                                                      outcome='success', events=events[index])
            else:
                result = writers[index].mark_completed(command_id=invocation.invocation_id, digest=digest,
                                                       outcome='failure', terminal_events=events[index],
                                                       failure_code=f'writer_{index}')
            return ('won', index, result)
        except RuntimeError as exc:
            assert 'immutable' in str(exc)
            return ('lost', index, None)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert sorted(r[0] for r in results) == ['lost', 'won']
    winner = next(r for r in results if r[0] == 'won')
    row = ExecutionStateStore(path).get(invocation.invocation_id)
    assert row == winner[2]
    assert (row.captured_events if kind == 'capture' else row.terminal_events) == events[winner[1]]
    print(json.dumps({'probe':'concurrent-immutable-writers', 'stage':kind, 'commits':1, 'conflicts':1}))


@pytest.mark.asyncio
async def test_simultaneous_redelivery_after_capture_does_not_infer_unknown_while_active(native, tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    ready, resume = asyncio.Event(), asyncio.Event()
    executions = 0
    async def execute(event):
        nonlocal executions
        executions += 1
        result = native_finish(native)
        ready.set()
        await resume.wait()
        return result['final_response']
    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    original = FakeMessage(valid_command)
    running = asyncio.create_task(adapter._handle_broker_message(original))
    await asyncio.wait_for(ready.wait(), 3)
    pending = adapter.execution_state.get(valid_command['command_id'])
    duplicate = FakeMessage(valid_command)
    await adapter._handle_broker_message(duplicate)
    assert duplicate.nacked == 1 and duplicate.acked == 0
    assert adapter.execution_state.get(valid_command['command_id']) == pending
    assert pending.state == 'started' and pending.outcome is None
    assert not pending.terminal_events
    resume.set()
    await asyncio.wait_for(running, 3)
    assert executions == 1 and original.acked == 1
    assert sum(e['type'] == contract.MESSAGE_APPENDED for e in adapter._js.events) == 1
    print(json.dumps({'probe':'active-captured-redelivery', 'executions':1, 'duplicate_nak':1, 'final_outcome':'success'}))


@pytest.mark.asyncio
async def test_ack_accepted_then_ack_reply_lost_replays_identical_success(native, tmp_path, valid_command):
    adapter = make_adapter(tmp_path)
    async def execute(event):
        return native_finish(native)['final_response']
    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    message = FakeMessage(valid_command)
    ack = message.ack
    async def uncertain_ack():
        await ack()  # Broker might have received it even if caller then errors.
        raise OSError('quality connection lost after command ACK send')
    message.ack = uncertain_ack
    await adapter._handle_broker_message(message)
    row = adapter.execution_state.get(valid_command['command_id'])
    expected = [wire(e) for e in (*row.started_events, *row.terminal_events)]
    assert message.acked == 1 and message.nacked == 1
    assert row.outcome == 'success'
    for _ in range(2):
        reopened = make_adapter(tmp_path)
        async def forbidden(event):
            pytest.fail('inference rerun after uncertain ACK')
        reopened.set_message_handler(FixtureRunner(forbidden).handle_message)
        replay = FakeMessage(valid_command)
        await reopened._handle_broker_message(replay)
        assert replay.acked == 1 and replay.nacked == 0
        assert [wire(e) for e in reopened._js.events] == expected
    print(json.dumps({'probe':'uncertain-command-ack', 'success_immutable':True, 'exact_replays':2}))


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['id', 'correlationid', 'issuer', 'body', 'timestamp', 'native'])
async def test_identity_body_time_native_conflicts_do_not_rewrite_captured_row(tmp_path, valid_command, field):
    adapter = make_adapter(tmp_path)
    async def execute(event):
        capture()
        return 'receipt'
    adapter.set_message_handler(FixtureRunner(execute).handle_message)
    original = FakeMessage(valid_command)
    await adapter._handle_broker_message(original)
    row = adapter.execution_state.get(valid_command['command_id'])
    assert original.acked == 1
    if field in {'id', 'correlationid', 'issuer'}:
        collision = copy.deepcopy(valid_command)
        if field == 'issuer':
            collision['actor']['agent_id'] = 'other-issuer'
        else:
            import uuid
            collision[field] = str(uuid.uuid4())
        delivery = FakeMessage(collision)
        await adapter._handle_broker_message(delivery)
        assert delivery.termed == 1 and delivery.acked == 0
    else:
        changed = copy.deepcopy(row.captured_events)
        if field == 'body':
            changed[0]['data']['text'] += '\nchanged'
        elif field == 'timestamp':
            changed[0]['time'] = '2026-11-17T00:00:00Z'
        else:
            changed[0]['data']['native_turn_id'] = 'different-observed-native-turn'
        with pytest.raises(RuntimeError, match='immutable'):
            adapter.execution_state.mark_captured(command_id=row.command_id,
                digest=row.envelope_digest, outcome='success', events=changed)
    assert ExecutionStateStore(adapter.execution_state.path).get(row.command_id) == row
    print(json.dumps({'probe':'lineage-bytes-conflict', 'field':field, 'unchanged':True}))
