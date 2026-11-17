"""Journal v4 adds capture durability without rewriting old results."""
from __future__ import annotations

import json
import sqlite3

import pytest
from bloodbank_hermes_gateway.contract import Invocation, started_events
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


def test_future_journal_version_refused_without_downgrade(tmp_path):
    path = tmp_path / "future.sqlite3"
    ExecutionStateStore(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 5")
    with pytest.raises(RuntimeError, match="schema version 5 is unsupported"):
        ExecutionStateStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
