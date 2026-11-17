"""Journal v4 adds capture durability without rewriting old results."""
from __future__ import annotations

import copy
import json
import sqlite3

import pytest
from bloodbank_hermes_gateway.contract import (
    Invocation,
    final_answer_event,
    started_events,
    terminal_events,
    validate_fact,
)
from bloodbank_hermes_gateway.execution_state import (
    ExecutionStateStore,
    envelope_digest,
)
from test_final_answer import BODY


def test_v3_upgrade_preserves_terminal_bytes_and_lane_metadata(tmp_path):
    path = tmp_path / "v3.sqlite3"
    # Use unusual whitespace to prove the upgrade does not reserialize outboxes.
    serialized = '[ { "id" : "old-result", "data": {"text": "old"} } ]'
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE executions (
                command_id TEXT PRIMARY KEY, envelope_digest TEXT NOT NULL,
                profile TEXT NOT NULL, state TEXT NOT NULL, outcome TEXT,
                started_events TEXT NOT NULL, terminal_events TEXT,
                rejection_reason TEXT, rejected_at TEXT, lane TEXT,
                failure_code TEXT, detached_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            PRAGMA user_version = 3;
        """)
        db.execute(
            "INSERT INTO executions VALUES "
            "('old', 'digest', 'research', 'completed', 'failure', '[]', ?, "
            "NULL, NULL, 'interactive', 'turn_timeout', 'detached-time', 'then', 'then')",
            (serialized,),
        )
    store = ExecutionStateStore(path)
    row = store.get("old")
    assert row is not None
    assert row.state == "completed" and row.outcome == "failure"
    assert row.lane == "interactive" and row.failure_code == "turn_timeout"
    assert row.detached_at == "detached-time"
    assert row.capture_outcome is None and row.captured_events == ()
    assert row.terminal_events == tuple(json.loads(serialized))
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert db.execute("SELECT terminal_events FROM executions").fetchone()[0] == serialized
    assert ExecutionStateStore(path).get("old") == row


def test_v4_pending_capture_survives_reopen_and_cannot_start_again(tmp_path, valid_command):
    path = tmp_path / "captured.sqlite3"
    store = ExecutionStateStore(path)
    invocation = Invocation.from_envelope(valid_command, "bloodbank-pm")
    digest = envelope_digest(valid_command)
    store.claim_pending(
        command_id=invocation.invocation_id, digest=digest, profile=invocation.profile,
        started_events=started_events(invocation),
    )
    store.mark_started(command_id=invocation.invocation_id, digest=digest)
    from bloodbank_hermes_gateway.contract import final_answer_event

    events = (final_answer_event(
        invocation, role="assistant", final_answer=True, text=BODY,
        native_session_id="real-session", native_turn_id="real-turn",
    ),)
    captured = store.mark_captured(
        command_id=invocation.invocation_id, digest=digest, outcome="success", events=events,
    )
    restarted = ExecutionStateStore(path)
    assert restarted.get(invocation.invocation_id) == captured
    assert captured.state == "started" and captured.outcome is None
    assert captured.terminal_events == ()
    with pytest.raises(RuntimeError, match="cannot be dispatched again"):
        restarted.mark_started(command_id=invocation.invocation_id, digest=digest)
    with pytest.raises(RuntimeError, match="immutable"):
        restarted.mark_captured(
            command_id=invocation.invocation_id, digest=digest, outcome="cancelled", events=(),
        )
    assert restarted.get(invocation.invocation_id) == captured


def _reverse_object_keys(value):
    if isinstance(value, dict):
        return {key: _reverse_object_keys(item) for key, item in reversed(value.items())}
    if isinstance(value, list):
        return [_reverse_object_keys(item) for item in value]
    return value


@pytest.mark.parametrize("stage", ["capture", "completed"])
@pytest.mark.parametrize(
    "original_value,conflicting_value",
    [(True, 1), (1, True), (1, 1.0), (1.0, 1)],
    ids=["bool-int", "int-bool", "int-float", "float-int"],
)
def test_immutable_events_compare_persisted_json_not_python_values(
    tmp_path, valid_command, stage, original_value, conflicting_value
):
    path = tmp_path / "canonical-immutable.sqlite3"
    store = ExecutionStateStore(path)
    valid_command["actor"]["metadata"] = {
        "nested": [{"value": original_value, "label": "issuer café 🐦\n"}]
    }
    invocation = Invocation.from_envelope(valid_command, "bloodbank-pm")
    digest = envelope_digest(valid_command)
    store.claim_pending(
        command_id=invocation.invocation_id, digest=digest, profile=invocation.profile,
        started_events=started_events(invocation),
    )
    store.mark_started(command_id=invocation.invocation_id, digest=digest)
    answer = final_answer_event(
        invocation, role="assistant", final_answer=True, text=BODY,
        native_session_id="native-session", native_turn_id="native-turn",
    )
    events = (answer,) if stage == "capture" else (
        answer, *terminal_events(invocation, outcome="success")
    )

    def persist(target, batch):
        if stage == "capture":
            return target.mark_captured(
                command_id=invocation.invocation_id, digest=digest,
                outcome="success", events=batch,
            )
        return target.mark_completed(
            command_id=invocation.invocation_id, digest=digest,
            outcome="success", terminal_events=batch,
        )

    def raw_row():
        with sqlite3.connect(path) as db:
            return db.execute("SELECT * FROM executions").fetchone()

    stored = persist(store, events)
    before = raw_row()
    reopened = ExecutionStateStore(path)
    assert persist(reopened, events) == stored
    reordered = tuple(_reverse_object_keys(event) for event in events)
    assert list(reordered[0]) != list(events[0])
    assert persist(reopened, reordered) == stored
    assert raw_row() == before

    conflicting = copy.deepcopy(events)
    conflicting[0]["data"]["issuer"]["metadata"]["nested"][0]["value"] = conflicting_value
    assert conflicting == events  # Python equality is not wire equality.
    assert conflicting[0]["id"] == events[0]["id"]
    assert json.dumps(conflicting, sort_keys=True) != json.dumps(events, sort_keys=True)
    for event in (*events, *conflicting):
        validate_fact(event)
    with pytest.raises(RuntimeError, match="immutable"):
        persist(reopened, conflicting)
    assert raw_row() == before
    result = ExecutionStateStore(path).get(invocation.invocation_id)
    assert result is not None
    replay = result.captured_events if stage == "capture" else result.terminal_events
    assert json.dumps(replay, sort_keys=True) == json.dumps(events, sort_keys=True)


def test_future_journal_version_refused_without_downgrade(tmp_path):
    path = tmp_path / "future.sqlite3"
    ExecutionStateStore(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 5")
    with pytest.raises(RuntimeError, match="schema version 5 is unsupported"):
        ExecutionStateStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
