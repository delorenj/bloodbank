"""Durable, payload-free hook execution receipts and read-only projections.

Claims prevent a delivered invocation from running a handler twice. A crash
does not cause arbitrary shell side effects to be replayed: interrupted work is
marked failed on startup. The journal never stores hook input or output.
"""
from __future__ import annotations

import json
import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from facts import (INVOCATION_TYPE, SNAPSHOT_TYPE, MAX_TIMELINE, HEARTBEAT_KEYS, envelope,
                   expires_at, serialize, snapshot_projection)

SCHEMA_VERSION = 1
TERMINAL = frozenset({"succeeded", "failed", "timed_out", "skipped", "interrupted"})
PENDING_EXECUTION = frozenset({"selected", "started"})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def identifier(value: Any) -> str:
    """Identifiers are scalars only, bounded independently of input size."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    raw = str(value)
    if len(raw) > 256 or "\x00" in raw:
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    return raw


def payload_sources(req: dict[str, Any]) -> list[dict]:
    payload = req.get("payload")
    if not isinstance(payload, dict):
        return []
    return [item for item in (payload, payload.get("extra"), payload.get("properties"))
            if isinstance(item, dict)]


def payload_identifier(req: dict[str, Any], names: tuple[str, ...]) -> str:
    for source in payload_sources(req):
        for name in names:
            found = identifier(source.get(name))
            if found:
                return found
    return ""


def native_session_id(req: dict[str, Any]) -> str:
    for source in (*payload_sources(req), req):
        for key in ("session_id", "sessionId", "sessionID", "thread_id", "threadId"):
            found = identifier(source.get(key))
            if found:
                return found
    env = req.get("env") if isinstance(req.get("env"), dict) else {}
    return identifier(env.get("CLAUDE_CODE_SESSION_ID") or env.get("CODEX_THREAD_ID"))


def invocation_identity(req: dict[str, Any]) -> tuple[str, str]:
    """Use a real event identity; repeated text is never an identity.

    Tool call ids are scoped by native event so pre/post each dispatch once.
    Events lacking a stable upstream id get a fresh UUID, explicitly labelled
    generated so observability does not claim cross-process deduplication.
    """
    key = identifier(req.get("invocation_id"))
    kind = "provided"
    if not key:
        key = payload_identifier(req, ("hook_event_id", "event_id", "eventId", "invocation_id"))
        if key:
            kind = "native_event"
    if not key and str(req.get("native", "")).lower() in {
        "pretooluse", "posttooluse", "posttoolusefailure", "pretoolusefailure",
        "on_tool_start", "on_tool_end", "beforetool", "aftertool",
        "tool.execute.before", "tool.execute.after", "pre_tool_use", "post_tool_use",
        "pre_tool_call", "post_tool_call",
    }:
        key = payload_identifier(req, ("tool_use_id", "tool_call_id", "toolCallId", "call_id", "callID"))
        if key:
            kind = "tool_call"
    if not key:
        return str(uuid.uuid4()), "generated"
    scope = json.dumps([req.get("cli"), req.get("native"), native_session_id(req), key])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "33god:hook:" + scope)), kind


class ReceiptStore:
    def __init__(self, path: Path, *, debounce: float = 0.0, max_delay: float = 10.0,
                 clock: Callable[[], float] = time.time) -> None:
        """`debounce` is the quiet window an unsettled invocation waits before
        its latest revision becomes publishable; `max_delay` caps that wait
        from the first unpublished change. Both are seconds on `clock`.
        """
        self.debounce = max(float(debounce), 0.0)
        self.max_delay = max(float(max_delay), self.debounce)
        self.clock = clock
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS invocations (
                    invocation_id TEXT PRIMARY KEY,
                    cli TEXT NOT NULL, native TEXT NOT NULL, role TEXT,
                    event_type TEXT, session_id TEXT, identity_kind TEXT NOT NULL,
                    received_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'received', deduplicated INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS executions (
                    invocation_id TEXT NOT NULL REFERENCES invocations ON DELETE CASCADE,
                    handler_id TEXT NOT NULL, mode TEXT NOT NULL,
                    status TEXT NOT NULL, reason TEXT, selected_at TEXT NOT NULL,
                    started_at TEXT, finished_at TEXT, duration_ms REAL, exit_code INTEGER,
                    event_id TEXT, event_type TEXT, publish_status TEXT,
                    PRIMARY KEY (invocation_id, handler_id)
                );
                CREATE TABLE IF NOT EXISTS receipt_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    invocation_id TEXT NOT NULL REFERENCES invocations ON DELETE CASCADE,
                    handler_id TEXT, status TEXT NOT NULL, reason TEXT, at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS invocation_recent ON invocations(received_at DESC);
                CREATE INDEX IF NOT EXISTS invocation_cli ON invocations(cli, native, received_at DESC);
                CREATE INDEX IF NOT EXISTS receipt_invocation ON receipt_events(invocation_id, sequence);
                CREATE TABLE IF NOT EXISTS observation_metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observation_outbox (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE, envelope TEXT,
                    created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS observation_revisions (
                    invocation_id TEXT PRIMARY KEY REFERENCES invocations ON DELETE CASCADE,
                    sequence INTEGER NOT NULL
                );
            """)
            # Additive v3 outbox columns. A pending row that carries a
            # coalesce key is replaced by the next revision of the same thing
            # instead of queueing behind it; due_at defers unsettled work.
            columns = {row[1] for row in db.execute("PRAGMA table_info(observation_outbox)")}
            for name, kind in (("coalesce_key", "TEXT"), ("due_at", "REAL"), ("deadline", "REAL")):
                if name not in columns:
                    try:
                        db.execute(f"ALTER TABLE observation_outbox ADD COLUMN {name} {kind}")
                    except sqlite3.OperationalError as exc:
                        # Another opener of the same journal migrated first.
                        if "duplicate column" not in str(exc):
                            raise
            db.execute("CREATE INDEX IF NOT EXISTS outbox_coalesce ON observation_outbox(coalesce_key)")
            db.execute("PRAGMA user_version = 3")
            db.execute("INSERT OR IGNORE INTO observation_metadata(key,value) VALUES('hub_id',?)", (str(uuid.uuid4()),))
            self.hub_id = db.execute("SELECT value FROM observation_metadata WHERE key='hub_id'").fetchone()[0]
        self.path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            # sqlite's transaction context alone does not close a connection.
            # A continuous publisher must not wait for cyclic GC to release
            # the handles/caches from thousands of receipt and outbox writes.
            db.close()

    def recover(self) -> None:
        """Previously started side effects are uncertain; never replay them."""
        at = now_iso()
        with self.connect() as db:
            rows = db.execute("SELECT invocation_id, handler_id FROM executions WHERE status IN ('selected','started')").fetchall()
            for row in rows:
                db.execute("UPDATE executions SET status='failed',reason='hub_restarted',finished_at=? WHERE invocation_id=? AND handler_id=?", (at, *row))
                db.execute("INSERT INTO receipt_events(invocation_id,handler_id,status,reason,at) VALUES(?,?,'failed','hub_restarted',?)", (*row, at))
                self._finish(db, row[0], at)
                self._observe(db, row[0], at)
            for row in db.execute("SELECT invocation_id FROM invocations WHERE status='received'").fetchall():
                db.execute("UPDATE invocations SET status='failed',updated_at=? WHERE invocation_id=?", (at, row[0]))
                db.execute("INSERT INTO receipt_events(invocation_id,status,reason,at) VALUES(?,'failed','hub_restarted',?)", (row[0], at))
                self._observe(db, row[0], at)

    def claim(self, invocation: dict[str, Any]) -> bool:
        at = now_iso()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute("""INSERT OR IGNORE INTO invocations
                (invocation_id,cli,native,role,event_type,session_id,identity_kind,received_at,updated_at)
                VALUES(:invocation_id,:cli,:native,:role,:event_type,:session_id,:identity_kind,:received_at,:received_at)""", invocation)
            fresh = cur.rowcount == 1
            if not fresh:
                db.execute("UPDATE invocations SET deduplicated=deduplicated+1, updated_at=? WHERE invocation_id=?", (at, invocation["invocation_id"]))
            db.execute("INSERT INTO receipt_events(invocation_id,status,at) VALUES(?,?,?)", (invocation["invocation_id"], "received" if fresh else "deduplicated", at))
            self._observe(db, invocation["invocation_id"], at)
            return fresh

    def select(self, iid: str, handler_id: str, mode: str, *, reason: str | None = None) -> None:
        at = now_iso()
        status = "skipped" if reason else "selected"
        with self.connect() as db:
            cur = db.execute("""INSERT OR IGNORE INTO executions
                (invocation_id,handler_id,mode,status,reason,selected_at,finished_at)
                VALUES(?,?,?,?,?,?,?)""", (iid, handler_id, mode, status, reason, at, at if reason else None))
            if cur.rowcount:
                db.execute("INSERT INTO receipt_events(invocation_id,handler_id,status,reason,at) VALUES(?,?,?,?,?)", (iid, handler_id, status, reason, at))
                db.execute("UPDATE invocations SET updated_at=? WHERE invocation_id=?", (at, iid))
                self._observe(db, iid, at)

    def update(self, iid: str, handler_id: str, status: str, *, reason: str | None = None,
               duration_ms: float | None = None, exit_code: int | None = None,
               event_id: str | None = None, event_type: str | None = None,
               publish_status: str | None = None) -> None:
        if status != "started" and status not in TERMINAL:
            raise ValueError("invalid execution status")
        at = now_iso()
        with self.connect() as db:
            db.execute("""UPDATE executions SET status=?, reason=?,
                started_at=CASE WHEN ?='started' THEN ? ELSE started_at END,
                finished_at=CASE WHEN ?='started' THEN NULL ELSE ? END,
                duration_ms=?,exit_code=?,event_id=?,event_type=?,publish_status=?
                WHERE invocation_id=? AND handler_id=?""",
                (status, reason, status, at, status, at, duration_ms, exit_code,
                 event_id, event_type, publish_status, iid, handler_id))
            db.execute("INSERT INTO receipt_events(invocation_id,handler_id,status,reason,at) VALUES(?,?,?,?,?)", (iid, handler_id, status, reason, at))
            self._finish(db, iid, at)
            self._observe(db, iid, at)

    def interrupt(self, iid: str, handler_id: str, reason: str = "shutdown_grace_expired") -> None:
        """Cancellation cannot overwrite a terminal transport receipt."""
        at = now_iso()
        with self.connect() as db:
            cur = db.execute("""UPDATE executions SET status='interrupted',reason=?,finished_at=?,
                publish_status=CASE WHEN handler_id='bloodbank-publisher' THEN 'unknown' ELSE publish_status END
                WHERE invocation_id=? AND handler_id=? AND status IN ('selected','started')""",
                (reason, at, iid, handler_id))
            if cur.rowcount:
                db.execute("INSERT INTO receipt_events(invocation_id,handler_id,status,reason,at) VALUES(?,?,'interrupted',?,?)", (iid, handler_id, reason, at))
                self._finish(db, iid, at)
                self._observe(db, iid, at)

    def interrupt_pending(self, reason: str = "shutdown_grace_expired") -> None:
        # A request can be canceled between claim and complete selection, or a
        # queued coroutine before its body starts. Finalize those receipts too.
        with self.connect() as db:
            pending = db.execute("SELECT invocation_id,handler_id FROM executions WHERE status IN ('selected','started')").fetchall()
        for row in pending:
            self.interrupt(*row, reason=reason)
        at = now_iso()
        with self.connect() as db:
            rows = db.execute("SELECT invocation_id FROM invocations WHERE status='received'").fetchall()
            for row in rows:
                db.execute("INSERT INTO receipt_events(invocation_id,status,reason,at) VALUES(?,'interrupted',?,?)", (row[0], reason, at))
                db.execute("UPDATE invocations SET status='interrupted',updated_at=? WHERE invocation_id=?", (at, row[0]))
                self._observe(db, row[0], at)

    @staticmethod
    def _finish(db: sqlite3.Connection, iid: str, at: str) -> None:
        db.execute("UPDATE invocations SET updated_at=? WHERE invocation_id=?", (at, iid))
        statuses = [r[0] for r in db.execute("SELECT status FROM executions WHERE invocation_id=?", (iid,))]
        if any(s in {"selected", "started"} for s in statuses):
            return
        result = ("failed" if any(s in {"failed", "timed_out"} for s in statuses)
                  else "interrupted" if "interrupted" in statuses
                  else "succeeded" if "succeeded" in statuses else "skipped")
        db.execute("UPDATE invocations SET status=?,updated_at=? WHERE invocation_id=?", (result, at, iid))

    def finish(self, iid: str) -> None:
        with self.connect() as db:
            at = now_iso()
            before = db.execute("SELECT status FROM invocations WHERE invocation_id=?", (iid,)).fetchone()
            if not before or before[0] != "received" or db.execute("SELECT 1 FROM executions WHERE invocation_id=? AND status IN ('selected','started') LIMIT 1", (iid,)).fetchone():
                return
            self._finish(db, iid, at)
            after = db.execute("SELECT status FROM invocations WHERE invocation_id=?", (iid,)).fetchone()
            if before and after and before[0] != after[0]:
                db.execute("INSERT INTO receipt_events(invocation_id,status,at) VALUES(?,?,?)", (iid, after[0], at))
                self._observe(db, iid, at)

    def _enqueue(self, db: sqlite3.Connection, ce_type: str, data: dict, at: str, *,
                 key: str | None = None, settled: bool = True) -> int:
        """Queue one immutable revision; supersede an unpublished one of `key`.

        One native hook used to leave ~13 revisions on the bus (claim, every
        handler's select/start/finish), each a full projection, although every
        consumer keeps only the newest revision per invocation. A pending row
        with the same key is deleted in the same transaction and replaced by a
        NEW sequence, so revisions stay monotonic and a row already in flight
        is simply superseded (its late delete is a no-op). Unsettled work waits
        for `debounce` seconds of quiet, never longer than `max_delay` from its
        first unpublished change; settled work is due at once.
        """
        now = self.clock()
        deadline = now + self.max_delay
        if key is not None:
            prior = db.execute("SELECT MIN(deadline) FROM observation_outbox WHERE coalesce_key=?", (key,)).fetchone()[0]
            if prior is not None:
                deadline = min(deadline, prior)
            db.execute("DELETE FROM observation_outbox WHERE coalesce_key=?", (key,))
        due = now if settled or not self.debounce else min(deadline, now + self.debounce)
        cur = db.execute("INSERT INTO observation_outbox(created_at,coalesce_key,due_at,deadline) VALUES(?,?,?,?)",
                         (at, key, due, deadline))
        sequence = cur.lastrowid
        fact = envelope(self.hub_id, sequence, ce_type, data, at)
        db.execute("UPDATE observation_outbox SET event_id=?,envelope=? WHERE sequence=?", (fact["id"], serialize(fact), sequence))
        return sequence

    def _observe(self, db: sqlite3.Connection, iid: str, at: str, *, backfill: bool = False) -> None:
        # Read using the SAME transaction as the receipt mutation. A process
        # crash can leave both committed or neither, never a missing fact.
        item = dict(db.execute("SELECT * FROM invocations WHERE invocation_id=?", (iid,)).fetchone())
        item["executions"] = [dict(r) for r in db.execute("SELECT * FROM executions WHERE invocation_id=? ORDER BY selected_at,handler_id", (iid,))]
        timeline = db.execute("SELECT sequence,handler_id,status,reason,at FROM receipt_events WHERE invocation_id=? ORDER BY sequence DESC LIMIT ?", (iid, MAX_TIMELINE)).fetchall()
        item["timeline"] = [dict(r) for r in reversed(timeline)]
        item["timeline_total"] = db.execute("SELECT COUNT(*) FROM receipt_events WHERE invocation_id=?", (iid,)).fetchone()[0]
        item["timeline_truncated"] = item["timeline_total"] > len(timeline)
        data = {"invocation": item, **({"backfill": True} if backfill else {})}
        settled = item["status"] != "received" and not any(
            execution["status"] in PENDING_EXECUTION for execution in item["executions"])
        sequence = self._enqueue(db, INVOCATION_TYPE, data, at, key="invocation:" + iid, settled=settled)
        db.execute("INSERT INTO observation_revisions(invocation_id,sequence) VALUES(?,?) ON CONFLICT(invocation_id) DO UPDATE SET sequence=excluded.sequence", (iid, sequence))

    def observe_snapshot(self, snapshot: dict, *, heartbeat: bool = False) -> int:
        snapshot = snapshot_projection(snapshot)
        at = snapshot["generated_at"]
        key = "heartbeat" if heartbeat else "snapshot"
        if heartbeat:
            snapshot = {key: value for key, value in snapshot.items() if key in HEARTBEAT_KEYS}
        with self.connect() as db:
            # A newer snapshot or heartbeat makes an unpublished one of the same
            # kind worthless; after a broker outage only the latest goes out.
            return self._enqueue(db, SNAPSHOT_TYPE, {key: snapshot, "expires_at": expires_at(at)}, at,
                                 key="system:" + key)

    def backfill(self, limit: int = 50) -> int:
        """Latest old projections, in bounded resumable transactions.

        A concurrent new mutation also creates the marker, so old state can
        never overwrite its newer projection. Pruning cannot delete pending
        facts because the outbox deliberately has no invocation foreign key.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT i.invocation_id FROM invocations i
                LEFT JOIN observation_revisions r USING(invocation_id)
                WHERE r.invocation_id IS NULL ORDER BY i.received_at DESC,i.invocation_id
                LIMIT ?""", (min(max(limit, 1), 200),)).fetchall()
            for row in rows:
                self._observe(db, row[0], now_iso(), backfill=True)
            return len(rows)

    def pending_observations(self, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM observation_outbox ORDER BY sequence LIMIT ?", (min(max(limit, 1), 200),))]

    def due_observations(self, limit: int = 20) -> list[dict]:
        """Pending rows whose debounce has elapsed; pre-v3 rows are always due."""
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM observation_outbox WHERE due_at IS NULL OR due_at<=? ORDER BY sequence LIMIT ?",
                (self.clock(), min(max(limit, 1), 200)))]

    def next_due_in(self) -> float | None:
        """Seconds until the earliest pending row is due; None when none is pending."""
        with self.connect() as db:
            row = db.execute("SELECT COUNT(*), MIN(COALESCE(due_at, 0)) FROM observation_outbox").fetchone()
        if not row[0]:
            return None
        return max(row[1] - self.clock(), 0.0)

    def observation_sent(self, sequence: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM observation_outbox WHERE sequence=?", (sequence,))
            for key, value in (("last_acked_sequence", str(sequence)), ("last_acked_at", now_iso())):
                db.execute("INSERT INTO observation_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def observation_failed(self, sequence: int, reason: str) -> None:
        # Exception class/reason token only; never exception messages carrying
        # server replies, payloads, endpoints or credentials.
        with self.connect() as db:
            db.execute("UPDATE observation_outbox SET attempts=attempts+1,last_error=? WHERE sequence=?", (reason, sequence))

    def observation_status(self) -> dict:
        with self.connect() as db:
            pending = db.execute("SELECT COUNT(*) FROM observation_outbox").fetchone()[0]
            metadata = dict(db.execute("SELECT key,value FROM observation_metadata"))
            error = db.execute("SELECT last_error FROM observation_outbox WHERE last_error IS NOT NULL ORDER BY sequence LIMIT 1").fetchone()
        return {"pending": pending, "last_acked_at": metadata.get("last_acked_at"),
                "last_acked_sequence": int(metadata["last_acked_sequence"]) if "last_acked_sequence" in metadata else None,
                "error": error[0] if error else None}

    def detail(self, iid: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM invocations WHERE invocation_id=?", (iid,)).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["executions"] = [dict(r) for r in db.execute("SELECT * FROM executions WHERE invocation_id=? ORDER BY selected_at,handler_id", (iid,))]
            item["timeline"] = [dict(r) for r in db.execute("SELECT sequence,handler_id,status,reason,at FROM receipt_events WHERE invocation_id=? ORDER BY sequence", (iid,))]
            return item

    def history(self, *, cli: str = "", native: str = "", handler: str = "",
                status: str = "", limit: int = 100, offset: int = 0) -> dict[str, Any]:
        limit, offset = min(max(int(limit), 1), 500), max(int(offset), 0)
        where, params = [], []
        for name, val in (("cli", cli), ("native", native)):
            if val:
                where.append(f"i.{name}=?")
                params.append(val)
        execution_filters = []
        if handler:
            execution_filters.append("e.handler_id=?")
            params.append(handler)
        if status == "deduplicated":
            where.append("i.deduplicated>0")
        elif status:
            execution_filters.append("e.status=?")
            params.append(status)
        if execution_filters:
            where.append("EXISTS (SELECT 1 FROM executions e WHERE e.invocation_id=i.invocation_id AND " + " AND ".join(execution_filters) + ")")
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM invocations i" + clause, params).fetchone()[0]
            rows = db.execute("SELECT i.* FROM invocations i" + clause + " ORDER BY received_at DESC,invocation_id DESC LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["executions"] = [dict(r) for r in db.execute("SELECT * FROM executions WHERE invocation_id=? ORDER BY selected_at,handler_id", (item["invocation_id"],))]
                items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "next_offset": offset + limit if offset + limit < total else None}

    def summary(self) -> dict[str, Any]:
        with self.connect() as db:
            totals = dict(db.execute("SELECT status,COUNT(*) FROM invocations GROUP BY status").fetchall())
            counts = [dict(r) for r in db.execute("""SELECT cli,native,COUNT(*) AS invocations,
                MAX(received_at) AS last_received_at,SUM(deduplicated) AS deduplicated,
                SUM(status='failed') AS failed,SUM(status='interrupted') AS interrupted
                FROM invocations GROUP BY cli,native""")]
            executions = [dict(r) for r in db.execute("""SELECT e.handler_id,i.cli,e.status,COUNT(*) AS count,
                MAX(COALESCE(e.finished_at,e.started_at,e.selected_at)) AS last_at,
                AVG(e.duration_ms) AS mean_duration_ms
                FROM executions e JOIN invocations i USING(invocation_id)
                GROUP BY e.handler_id,i.cli,e.status""")]
            first = db.execute("SELECT MIN(received_at) FROM invocations").fetchone()[0]
        return {"totals": totals, "native_activity": counts, "handler_activity": executions,
                "observed_since": first}

    def prune(self, *, days: int = 30, max_invocations: int = 100000) -> None:
        before = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as db:
            db.execute("DELETE FROM invocations WHERE received_at<? AND status!='received'", (before,))
            db.execute("""DELETE FROM invocations WHERE invocation_id IN (
                SELECT invocation_id FROM invocations WHERE status!='received'
                ORDER BY received_at DESC LIMIT -1 OFFSET ?)""", (max_invocations,))
