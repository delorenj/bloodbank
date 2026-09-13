"""Durable, payload-free hook execution receipts and read-only projections.

Claims prevent a delivered invocation from running a handler twice. A crash
does not cause arbitrary shell side effects to be replayed: interrupted work is
marked failed on startup. The journal never stores hook input or output.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
TERMINAL = frozenset({"succeeded", "failed", "timed_out", "skipped"})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def identifier(value: Any) -> str:
    """Identifiers are scalars only, bounded independently of input size."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    return str(value).replace("\x00", "")[:256]


def native_session_id(req: dict[str, Any]) -> str:
    payload = req.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    for source in (payload, req):
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
    payload = req.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    key = identifier(req.get("invocation_id"))
    kind = "provided"
    if not key:
        for name in ("hook_event_id", "event_id", "eventId", "invocation_id"):
            key = identifier(payload.get(name))
            if key:
                kind = "native_event"
                break
    if not key and str(req.get("native", "")).lower() in {
        "pretooluse", "posttooluse", "posttoolusefailure", "pretoolusefailure",
        "on_tool_start", "on_tool_end", "beforetool", "aftertool",
        "tool.execute.before", "tool.execute.after", "pre_tool_use", "post_tool_use",
    }:
        for name in ("tool_use_id", "tool_call_id", "toolCallId", "call_id", "callID"):
            key = identifier(payload.get(name))
            if key:
                kind = "tool_call"
                break
    if not key:
        return str(uuid.uuid4()), "generated"
    scope = json.dumps([req.get("cli"), req.get("native"), native_session_id(req), key])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "33god:hook:" + scope)), kind


class ReceiptStore:
    def __init__(self, path: Path) -> None:
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
                PRAGMA user_version = 1;
            """)
        self.path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def recover(self) -> None:
        """Previously started side effects are uncertain; never replay them."""
        at = now_iso()
        with self.connect() as db:
            rows = db.execute("SELECT invocation_id, handler_id FROM executions WHERE status IN ('selected','started')").fetchall()
            for row in rows:
                db.execute("INSERT INTO receipt_events(invocation_id,handler_id,status,reason,at) VALUES(?,?,'failed','hub_restarted',?)", (*row, at))
            db.execute("UPDATE executions SET status='failed', reason='hub_restarted', finished_at=? WHERE status IN ('selected','started')", (at,))
            db.execute("UPDATE invocations SET status='failed', updated_at=? WHERE status='received'", (at,))

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

    @staticmethod
    def _finish(db: sqlite3.Connection, iid: str, at: str) -> None:
        statuses = [r[0] for r in db.execute("SELECT status FROM executions WHERE invocation_id=?", (iid,))]
        if any(s in {"selected", "started"} for s in statuses):
            return
        result = ("failed" if any(s in {"failed", "timed_out"} for s in statuses)
                  else "succeeded" if "succeeded" in statuses else "skipped")
        db.execute("UPDATE invocations SET status=?,updated_at=? WHERE invocation_id=?", (result, at, iid))

    def finish(self, iid: str) -> None:
        with self.connect() as db:
            self._finish(db, iid, now_iso())

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
                SUM(status='failed') AS failed FROM invocations GROUP BY cli,native""")]
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
