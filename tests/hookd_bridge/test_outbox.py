from __future__ import annotations

from pathlib import Path

from hookd_bridge.bridge import OutboxStore


def test_outbox_enqueue_and_publish_lifecycle(tmp_path: Path):
    db = tmp_path / "outbox.db"
    store = OutboxStore(str(db))
    store.setup()

    envelope = {
        "event_id": "evt-1",
        "event_type": "command.envelope",
        "payload": {"command_id": "cmd-1"},
    }

    row_id = store.enqueue(command_id="cmd-1", routing_key="command.cack.hook_dispatch", envelope=envelope)
    assert row_id > 0
    assert store.pending_count() == 1

    rows = store.fetch_due(limit=10)
    assert len(rows) == 1
    rid, command_id, rk, body, attempts = rows[0]
    assert rid == row_id
    assert command_id == "cmd-1"
    assert rk == "command.cack.hook_dispatch"
    assert attempts == 0
    assert b"command.envelope" in body

    store.mark_failed(row_id, attempts=0, err="temporary failure")
    rows_after_fail = store.fetch_due(limit=10)
    # Should backoff after failure; usually no immediate due rows
    assert len(rows_after_fail) == 0

    # Mark published and verify pending decreases
    store.mark_published(row_id)
    assert store.pending_count() == 0

    store.close()
