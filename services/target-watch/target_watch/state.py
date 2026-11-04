"""SQLite state: near-duplicate window, Gmail ids seen, and the sink's routing ledger.

One file (~/.local/state/target-watch/state.db) shared by the three
entrypoints; each opens its own short connections, WAL mode, so the matcher,
the sink and a gmail-poll run never block each other for long.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS detections (
  event_id TEXT PRIMARY KEY,
  watch_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  text_hash TEXT,
  received_epoch REAL NOT NULL,
  recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS detections_window ON detections(watch_id, provider, text_hash, received_epoch);
CREATE TABLE IF NOT EXISTS gmail_seen (
  message_id TEXT PRIMARY KEY,
  event_id TEXT,
  outcome TEXT NOT NULL,
  seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS routes (
  detection_id TEXT PRIMARY KEY,
  watch_id TEXT NOT NULL,
  conv_key TEXT NOT NULL,
  text_hash TEXT,
  received_epoch REAL NOT NULL,
  px_kind TEXT,
  px_state TEXT NOT NULL DEFAULT 'none',
  action TEXT,
  ticket_ref TEXT,
  reason TEXT,
  notified INTEGER NOT NULL DEFAULT 0,
  notify_channels TEXT NOT NULL DEFAULT '[]',
  envelope TEXT,
  published INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS routes_text ON routes(watch_id, text_hash, received_epoch);
CREATE TABLE IF NOT EXISTS tickets (
  watch_id TEXT NOT NULL,
  conv_key TEXT NOT NULL,
  ticket_ref TEXT NOT NULL,
  created_epoch REAL NOT NULL,
  PRIMARY KEY (watch_id, conv_key, ticket_ref)
);
"""

ROUTE_FIELDS = (
    "detection_id", "watch_id", "conv_key", "text_hash", "received_epoch", "px_kind", "px_state",
    "action", "ticket_ref", "reason", "notified", "notify_channels", "envelope", "published",
)


class State:
    def __init__(self, path: Path | str, *, read_only: bool = False):
        self.path = str(path)
        self.read_only = read_only
        if self.path != ":memory:" and not read_only:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._memory = sqlite3.connect(":memory:") if self.path == ":memory:" else None
        if not read_only:
            with self.connect() as db:
                db.executescript(SCHEMA)
        elif self._memory is None and not Path(self.path).exists():
            # A dry run against a state file that does not exist yet reads as empty.
            self._memory = sqlite3.connect(":memory:")
            self._memory.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        if self._memory is not None:
            with self._memory:
                yield self._memory
            return
        uri = f"file:{self.path}?mode=ro" if self.read_only else f"file:{self.path}"
        db = sqlite3.connect(uri, uri=True, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    # --- matcher ------------------------------------------------------------

    def near_duplicate(
        self, event_id: str, watch_id: str, provider: str, text_hash: str | None,
        received_epoch: float, window_s: float = 2.0,
    ) -> bool:
        """True when another detection of the same target/provider/text sits within window_s.

        The same event id is never its own duplicate: a redelivery after a failed
        publish must publish again (JetStream drops the copy by Nats-Msg-Id).
        """
        with self.connect() as db:
            if text_hash:
                row = db.execute(
                    "SELECT event_id FROM detections WHERE watch_id=? AND provider=? AND text_hash=? "
                    "AND event_id<>? AND abs(received_epoch-?)<=? LIMIT 1",
                    (watch_id, provider, text_hash, event_id, received_epoch, window_s),
                ).fetchone()
                if row:
                    return True
            if not self.read_only:
                db.execute(
                    "INSERT OR IGNORE INTO detections VALUES(?,?,?,?,?,?)",
                    (event_id, watch_id, provider, text_hash, received_epoch, time.time()),
                )
        return False

    def prune(self, older_than_s: float = 7 * 86400) -> None:
        if self.read_only:
            return
        cutoff = time.time() - older_than_s
        with self.connect() as db:
            db.execute("DELETE FROM detections WHERE recorded_at<?", (cutoff,))

    # --- gmail --------------------------------------------------------------

    def gmail_seen(self, message_id: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM gmail_seen WHERE message_id=?", (message_id,)).fetchone() is not None

    def mark_gmail(self, message_id: str, event_id: str | None, outcome: str) -> None:
        if self.read_only:
            return
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO gmail_seen VALUES(?,?,?,?)",
                (message_id, event_id, outcome, time.time()),
            )

    # --- sink ---------------------------------------------------------------

    def route(self, detection_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                f"SELECT {','.join(ROUTE_FIELDS)} FROM routes WHERE detection_id=?", (detection_id,)
            ).fetchone()
        if row is None:
            return None
        out = dict(zip(ROUTE_FIELDS, row))
        out["notify_channels"] = json.loads(out["notify_channels"] or "[]")
        out["envelope"] = json.loads(out["envelope"]) if out["envelope"] else None
        return out

    def open_route(self, detection_id: str, watch_id: str, conv_key: str, text_hash: str | None, received_epoch: float) -> None:
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO routes(detection_id,watch_id,conv_key,text_hash,received_epoch,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (detection_id, watch_id, conv_key, text_hash, received_epoch, now, now),
            )

    def update_route(self, detection_id: str, **fields) -> None:
        if not fields:
            return
        for key in fields:
            if key not in ROUTE_FIELDS:
                raise KeyError(key)
        values = []
        for key, value in fields.items():
            if key in {"notify_channels"}:
                value = json.dumps(list(value))
            elif key == "envelope":
                value = json.dumps(value, sort_keys=True)
            values.append(value)
        with self.connect() as db:
            db.execute(
                "UPDATE routes SET " + ",".join(f"{k}=?" for k in fields) + ",updated_at=? WHERE detection_id=?",
                (*values, time.time(), detection_id),
            )

    def recent_text_route(self, watch_id: str, text_hash: str, received_epoch: float, detection_id: str, window_s: float) -> str | None:
        """A different detection of the same target and text already routed inside the window."""
        with self.connect() as db:
            row = db.execute(
                "SELECT detection_id FROM routes WHERE watch_id=? AND text_hash=? AND detection_id<>? "
                "AND abs(received_epoch-?)<=? AND action IS NOT NULL AND action<>'skipped_duplicate' LIMIT 1",
                (watch_id, text_hash, detection_id, received_epoch, window_s),
            ).fetchone()
        return row[0] if row else None

    def coalesce_ticket(self, watch_id: str, conv_key: str, since_epoch: float) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT ticket_ref FROM tickets WHERE watch_id=? AND conv_key=? AND created_epoch>=? "
                "ORDER BY created_epoch DESC LIMIT 1",
                (watch_id, conv_key, since_epoch),
            ).fetchone()
        return row[0] if row else None

    def record_ticket(self, watch_id: str, conv_key: str, ticket_ref: str, created_epoch: float | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO tickets VALUES(?,?,?,?)",
                (watch_id, conv_key, ticket_ref, created_epoch if created_epoch is not None else time.time()),
            )
