"""Durability and event contract acceptance without live broker traffic."""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

HUB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUB))
from facts import (HEARTBEAT_KEYS, MAX_FACT_BYTES, MAX_TIMELINE,
                   configuration_fingerprint, snapshot_projection)
from observations import OutboxWorker, publish
from receipts import ReceiptStore, now_iso
from core.validate import validate_envelope
from test_hub import HubHarness, echo_handler


def invocation(iid="receipt-one", native="local-unmapped"):
    return {"invocation_id": iid, "cli": "codex", "native": native, "role": None,
            "event_type": None, "session_id": "metadata-only", "identity_kind": "provided",
            "received_at": now_iso()}


def facts(store):
    return [json.loads(row["envelope"]) for row in store.pending_observations(200)]


def snapshot():
    return {"schema_version": 1, "generated_at": now_iso(),
            "hub": {"state": "running", "started_at": now_iso(), "publish_enabled": True},
            "bindings": [], "handlers": [], "totals": {}, "native_activity": [],
            "handler_activity": [], "observed_since": None,
            "installed_inventory": {"generated_at": now_iso(), "status": "healthy", "clis": []}}


def test_receipt_burst_coalesces_into_one_immutable_latest_revision(tmp_path):
    """Consumers keep only the newest revision per invocation; the bus carries
    only that. Each mutation still gets a new, higher revision and identity."""
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    row = invocation()
    assert store.claim(row)
    first = facts(store)[0]
    assert first["data"]["invocation"]["executions"] == []
    store.select(row["invocation_id"], "retention", "async")
    store.update(row["invocation_id"], "retention", "started")
    store.update(row["invocation_id"], "retention", "timed_out", reason="timeout", duration_ms=30)
    assert not store.claim(row)
    result = facts(store)
    assert len(result) == 1
    latest = result[0]
    assert latest["id"] != first["id"]
    assert latest["data"]["revision"] == 5 > first["data"]["revision"]
    assert latest["data"]["invocation"]["deduplicated"] == 1
    assert latest["data"]["invocation"]["executions"][0]["status"] == "timed_out"
    assert [step["status"] for step in latest["data"]["invocation"]["timeline"]] == [
        "received", "selected", "started", "timed_out", "deduplicated"]
    validate_envelope(latest)
    assert latest["time"] == latest["data"]["invocation"]["timeline"][-1]["at"]
    # Invocations never coalesce with each other.
    store.claim(invocation("receipt-two"))
    assert [event["data"]["invocation"]["invocation_id"] for event in facts(store)] == [
        "receipt-one", "receipt-two"]


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def due(store):
    return [json.loads(row["envelope"]) for row in store.due_observations(200)]


def test_unsettled_invocation_waits_for_quiet_then_publishes_latest(tmp_path):
    clock = Clock()
    store = ReceiptStore(tmp_path / "receipts.sqlite3", debounce=2.0, max_delay=10.0, clock=clock)
    store.claim(invocation())
    store.select("receipt-one", "slow", "async")
    assert due(store) == []
    assert store.next_due_in() == pytest.approx(2.0)
    clock.now += 1.5
    store.update("receipt-one", "slow", "started")
    clock.now += 1.5
    assert due(store) == [], "each change restarts the quiet window"
    clock.now += 0.5
    [interim] = due(store)
    assert interim["data"]["invocation"]["status"] == "received"
    assert interim["data"]["invocation"]["executions"][0]["status"] == "started"


def test_settled_invocation_is_due_at_once(tmp_path):
    clock = Clock()
    store = ReceiptStore(tmp_path / "receipts.sqlite3", debounce=2.0, max_delay=10.0, clock=clock)
    store.claim(invocation())
    store.select("receipt-one", "quick", "async")
    store.update("receipt-one", "quick", "started")
    assert due(store) == []
    store.update("receipt-one", "quick", "succeeded", duration_ms=4)
    [settled] = due(store)
    assert settled["data"]["invocation"]["status"] == "succeeded"
    # All handlers skipped: finish() settles it too.
    store.claim(invocation("skipped"))
    store.select("skipped", "never", "async", reason="not_applicable")
    store.finish("skipped")
    assert [event["data"]["invocation"]["status"] for event in due(store)] == ["succeeded", "skipped"]


def test_continuous_changes_cannot_starve_publication_past_max_delay(tmp_path):
    clock = Clock()
    store = ReceiptStore(tmp_path / "receipts.sqlite3", debounce=2.0, max_delay=5.0, clock=clock)
    row = invocation()
    store.claim(row)
    store.select("receipt-one", "slow", "async")
    for _ in range(4):
        clock.now += 1.0
        assert not store.claim(row)
        assert due(store) == []
    clock.now += 1.0
    [capped] = due(store)
    assert capped["data"]["invocation"]["deduplicated"] == 4
    # Once that revision is acknowledged, the next change starts a fresh cap.
    store.observation_sent(store.due_observations(1)[0]["sequence"])
    store.claim(row)
    assert store.next_due_in() == pytest.approx(2.0)


def test_in_flight_revision_is_superseded_not_lost(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    store.claim(invocation())
    store.finish("receipt-one")
    delivered = []
    def publisher(event, body):
        # A hook mutates the invocation while this revision is on the wire.
        if not delivered:
            store.claim(invocation())
        delivered.append(event["data"]["revision"])
        return {"stream": "BLOODBANK_EVENTS", "seq": len(delivered)}
    worker = OutboxWorker(store, publisher=publisher)
    assert asyncio.run(worker.flush())
    assert asyncio.run(worker.flush())
    assert not store.pending_observations()
    assert len(delivered) == 2 and delivered[0] < delivered[1]


def test_worker_stops_scanning_for_backfill_once_a_pass_finds_nothing(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    with store.connect() as db:
        for i in range(3):
            row = invocation(f"old-{i}")
            db.execute("""INSERT INTO invocations(invocation_id,cli,native,role,event_type,
                session_id,identity_kind,received_at,updated_at,status)
                VALUES(:invocation_id,:cli,:native,:role,:event_type,:session_id,
                :identity_kind,:received_at,:received_at,'skipped')""", row)
    calls = []
    original = store.backfill
    def counted(limit=50):
        calls.append(limit)
        return original(2)
    store.backfill = counted
    published = []
    worker = OutboxWorker(store, publisher=lambda event, body: published.append(event["id"]))
    async def run():
        task = asyncio.create_task(worker.run())
        for _ in range(8):
            worker.wake.set()
            await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
    assert worker.backfill_complete
    assert len(calls) == 3, "2 + 1 + the empty pass, then never again"
    assert len(published) == 3


def test_newer_snapshot_or_heartbeat_supersedes_unpublished_one(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    store.observe_snapshot(snapshot())
    for _ in range(3):
        store.observe_snapshot(snapshot(), heartbeat=True)
    kinds = [next(k for k in ("snapshot", "heartbeat") if k in event["data"]) for event in facts(store)]
    assert kinds == ["snapshot", "heartbeat"]


def test_pre_coalescing_outbox_rows_migrate_and_stay_due(tmp_path):
    import sqlite3
    path = tmp_path / "receipts.sqlite3"
    store = ReceiptStore(path)
    store.claim(invocation())
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX outbox_coalesce")
        db.execute("ALTER TABLE observation_outbox DROP COLUMN deadline")
        db.execute("ALTER TABLE observation_outbox DROP COLUMN due_at")
        db.execute("ALTER TABLE observation_outbox DROP COLUMN coalesce_key")
    upgraded = ReceiptStore(path, debounce=2.0, clock=Clock())
    [legacy] = upgraded.due_observations(10)
    assert legacy["coalesce_key"] is None and legacy["due_at"] is None
    upgraded.claim(invocation("fresh"))
    assert len(upgraded.pending_observations(10)) == 2


def test_unmapped_skip_and_restart_recovery_are_bus_facts(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    store.claim(invocation("unmapped"))
    store.finish("unmapped")
    assert facts(store)[-1]["data"]["invocation"]["status"] == "skipped"
    store.claim(invocation("pending"))
    store.select("pending", "one", "async")
    store.update("pending", "one", "started")
    ReceiptStore(store.path).recover()
    last = facts(store)[-1]
    assert last["data"]["invocation"]["status"] == "failed"
    assert last["data"]["invocation"]["executions"][0]["reason"] == "hub_restarted"
    assert len(last["data"]["invocation"]["executions"]) == 1


def test_receipt_and_outbox_commit_or_rollback_together(tmp_path, monkeypatch):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    def fail(*args, **kwargs):
        raise OSError("simulated journal failure")
    monkeypatch.setattr(store, "_enqueue", fail)
    with pytest.raises(OSError):
        store.claim(invocation())
    assert store.detail("receipt-one") is None
    assert not store.pending_observations()


def test_backfill_is_bounded_resumable_and_cannot_overwrite_live_revision(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    with store.connect() as db:
        for i in range(7):
            row = invocation(f"old-{i}")
            db.execute("""INSERT INTO invocations(invocation_id,cli,native,role,event_type,
                session_id,identity_kind,received_at,updated_at,status)
                VALUES(:invocation_id,:cli,:native,:role,:event_type,:session_id,
                :identity_kind,:received_at,:received_at,'skipped')""", row)
    assert store.backfill(2) == 2
    original = facts(store)
    assert all(event["data"]["backfill"] for event in original)
    restarted = ReceiptStore(store.path)
    assert restarted.hub_id == store.hub_id
    assert restarted.backfill(2) == 2
    represented = {event["data"]["invocation"]["invocation_id"] for event in facts(store)}
    live = next(f"old-{i}" for i in range(7) if f"old-{i}" not in represented)
    assert not restarted.claim(invocation(live))
    assert restarted.backfill(200) == 2
    assert restarted.backfill(200) == 0
    assert len(facts(store)) == 7
    for event in original:
        assert event in facts(store)
    latest = next(event for event in facts(store) if event["data"]["invocation"]["invocation_id"] == live)
    assert latest["data"]["invocation"]["deduplicated"] == 1


def test_failed_publish_restart_and_redelivery_keep_exact_event_and_sequence(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    store.claim(invocation())
    before = store.pending_observations()[0]
    def offline(*args):
        raise ConnectionError("private endpoint must not be persisted")
    assert not asyncio.run(OutboxWorker(store, publisher=offline).flush())
    pending = ReceiptStore(store.path).pending_observations()[0]
    assert pending["envelope"] == before["envelope"]
    assert pending["event_id"] == before["event_id"]
    assert pending["last_error"] == "ConnectionError"
    delivered = []
    def success(event, body):
        delivered.append((event["id"], body))
        return {"stream": "BLOODBANK_EVENTS", "seq": 42}
    restarted = ReceiptStore(store.path)
    assert asyncio.run(OutboxWorker(restarted, publisher=success).flush())
    assert delivered == [(before["event_id"], before["envelope"])]
    assert not restarted.pending_observations()
    restarted.claim(invocation("next"))
    assert facts(restarted)[0]["data"]["sequence"] > before["sequence"]


def test_acknowledged_but_uncommitted_delivery_retries_same_identity(tmp_path, monkeypatch):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    store.claim(invocation())
    deliveries = []
    def delivered(event, body):
        deliveries.append((event["id"], body))
    def crash(*args):
        raise OSError("crash between PubAck and outbox removal")
    monkeypatch.setattr(store, "observation_sent", crash)
    with pytest.raises(OSError):
        asyncio.run(OutboxWorker(store, publisher=delivered).flush())
    restarted = ReceiptStore(store.path)
    assert asyncio.run(OutboxWorker(restarted, publisher=delivered).flush())
    assert len(deliveries) == 2 and deliveries[0] == deliveries[1]


def test_pending_fact_survives_local_receipt_retention(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    store.claim({**invocation(), "received_at": "2020-01-01T00:00:00Z"})
    store.finish("receipt-one")
    before = facts(store)
    store.prune()
    assert store.detail("receipt-one") is None
    assert facts(store) == before


def test_snapshot_heartbeat_schema_strips_arbitrary_config_and_expires_from_observation(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    source = snapshot()
    source["generated_at"] = "2026-01-01T00:00:00Z"
    source["env"] = {"SECRET": "must-not-publish"}
    source["hub"]["stdout"] = "must-not-publish"
    source["installed_inventory"]["command"] = "must-not-publish"
    store.observe_snapshot(source)
    store.observe_snapshot(source, heartbeat=True)
    full, heartbeat = facts(store)
    assert "must-not-publish" not in json.dumps([full, heartbeat])
    assert heartbeat["data"]["expires_at"] == "2026-01-01T00:01:30.000Z"
    assert "snapshot" not in heartbeat["data"]
    assert set(heartbeat["data"]["heartbeat"]) == set(HEARTBEAT_KEYS)
    validate_envelope(full)
    validate_envelope(heartbeat)
    bad = json.loads(json.dumps(full))
    bad["data"]["snapshot"]["hub"]["env"] = {"SECRET": "no"}
    with pytest.raises(Exception, match="Additional properties"):
        validate_envelope(bad)
    bad = json.loads(json.dumps(full))
    bad["data"]["heartbeat"] = heartbeat["data"]["heartbeat"]
    with pytest.raises(Exception):
        validate_envelope(bad)


def test_configuration_fingerprint_ignores_activity_and_observation_time():
    first = snapshot()
    second = snapshot()
    second["hub"]["state"] = "draining"
    second["totals"] = {"succeeded": 12}
    assert configuration_fingerprint(first) == configuration_fingerprint(second)
    second["handlers"] = [{"id": "new"}]
    assert configuration_fingerprint(first) != configuration_fingerprint(second)


def test_duplicate_storm_has_explicit_bounded_timeline(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    row = invocation()
    store.claim(row)
    # Populate past metadata directly to avoid hundreds of full sqlite fsyncs.
    with store.connect() as db:
        db.executemany("INSERT INTO receipt_events(invocation_id,status,at) VALUES(?,'deduplicated',?)", [(row["invocation_id"], now_iso())] * (MAX_TIMELINE + 1))
    store.claim(row)
    value = facts(store)[-1]
    projection = value["data"]["invocation"]
    assert projection["timeline_truncated"]
    assert projection["timeline_total"] == MAX_TIMELINE + 3
    assert len(projection["timeline"]) == MAX_TIMELINE
    assert len(json.dumps(value).encode()) < MAX_FACT_BYTES


def fake_broker(mode):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    captured = []
    def serve():
        with listener, listener.accept()[0] as peer:
            peer.settimeout(2)
            peer.sendall(b'INFO {"max_payload":1048576,"headers":true}\r\n')
            stream = peer.makefile("rb")
            while True:
                line = stream.readline()
                if line.startswith(b"HPUB "):
                    parts = line.split()
                    payload = stream.read(int(parts[-1]) + 2)
                    captured.append((parts, payload))
                    inbox = parts[2]
                    break
            peer.sendall(b"PONG\r\n")
            if mode == "pong":
                return
            ack = ({"error": {"code": 503}} if mode == "error" else
                   {"stream": "BLOODBANK_EVENTS", "seq": 42, "duplicate": True})
            body = json.dumps(ack).encode()
            frame = b"MSG " + inbox + b" 1 " + str(len(body)).encode() + b"\r\n" + body + b"\r\n"
            # Make the client handle fragmented PubAck payloads.
            peer.sendall(frame[:-3])
            time.sleep(0.01)
            peer.sendall(frame[-3:])
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, captured, thread


@pytest.mark.parametrize("mode", ["ack", "pong", "error"])
def test_publish_requires_correlated_jetstream_ack_and_sets_dedup_header(tmp_path, monkeypatch, mode):
    store = ReceiptStore(tmp_path / "receipts.sqlite3")
    store.claim(invocation())
    row = store.pending_observations()[0]
    fact = json.loads(row["envelope"])
    port, captured, thread = fake_broker(mode)
    monkeypatch.setenv("BLOODBANK_NATS_HOST", "127.0.0.1")
    monkeypatch.setenv("BLOODBANK_NATS_PORT", str(port))
    if mode == "ack":
        assert publish(fact, row["envelope"])["duplicate"] is True
    else:
        with pytest.raises(RuntimeError):
            publish(fact, row["envelope"])
    thread.join(2)
    assert not thread.is_alive()
    assert f"Nats-Msg-Id: {fact['id']}\r\n".encode() in captured[0][1]
    assert b"Nats-Expected-Stream: BLOODBANK_EVENTS\r\n" in captured[0][1]


def test_offline_observation_worker_does_not_block_or_repeat_native_hook(tmp_path):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        unavailable = reserved.getsockname()[1]
    env = {"HOOK_HUB_OBSERVATIONS_PUBLISH": "true", "BLOODBANK_ENABLED": "true",
           "BLOODBANK_NATS_HOST": "127.0.0.1", "BLOODBANK_NATS_PORT": str(unavailable)}
    with HubHarness(tmp_path, echo_handler("one", "CONTEXT"), env) as hub:
        started = time.monotonic()
        reply = hub.request("claude", "UserPromptSubmit", {"prompt": "PRIVATE-PROMPT"})
        assert time.monotonic() - started < 1.0
        assert reply["stdout"] == "CONTEXT"
        store = ReceiptStore(tmp_path / "receipts.sqlite3")
        assert store.observation_status()["pending"] > 0
        assert "PRIVATE-PROMPT" not in json.dumps(facts(store))
        receipt = store.detail(reply["invocation_id"])
        assert len(receipt["executions"]) == 1


def test_snapshot_failure_cannot_block_valid_receipt_delivery(tmp_path, monkeypatch):
    import hub
    monkeypatch.setattr(hub, "RECEIPT_PATH", tmp_path / "receipts.sqlite3")
    monkeypatch.setattr(hub, "LOG_PATH", tmp_path / "hub.log")
    # The claimed invocation below never settles; do not wait out the debounce.
    monkeypatch.setattr(hub, "OBSERVATION_DEBOUNCE", 0.0)
    delivered = threading.Event()
    def acknowledge(event, body):
        assert event["type"] == "bloodbank.agent.hook.updated"
        delivered.set()
    async def broken_status():
        raise ValueError("simulated inventory failure")
    async def run():
        server = hub.Server()
        server.store.claim(invocation())
        server.status = broken_status
        server.observations.publisher = acknowledge
        task = asyncio.create_task(server.observe())
        try:
            assert await asyncio.to_thread(delivered.wait, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
