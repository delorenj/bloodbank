"""SQLite state: near-duplicate window, Gmail ids seen, the sink's routing ledger, its notification log and
the detections waiting for a Create Ticket tap.

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
CREATE TABLE IF NOT EXISTS rings (
  watch_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  last_epoch REAL NOT NULL,
  PRIMARY KEY (watch_id, channel)
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  detection_id TEXT NOT NULL,
  watch_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  outcome TEXT NOT NULL,
  detail TEXT,
  ref TEXT,
  at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_detection ON notifications(detection_id);
CREATE TABLE IF NOT EXISTS buttons (
  detection_id TEXT PRIMARY KEY,
  watch_id TEXT NOT NULL,
  detection TEXT NOT NULL,
  channels TEXT NOT NULL DEFAULT '[]',
  state TEXT NOT NULL DEFAULT 'awaiting',
  px_kind TEXT,
  action TEXT,
  ticket_ref TEXT,
  ticket_url TEXT,
  reason TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
"""

ROUTE_FIELDS = (
    "detection_id", "watch_id", "conv_key", "text_hash", "received_epoch", "px_kind", "px_state",
    "action", "ticket_ref", "reason", "notified", "notify_channels", "envelope", "published",
)
BUTTON_FIELDS = (
    "detection_id", "watch_id", "detection", "channels", "state", "px_kind", "action", "ticket_ref", "ticket_url",
    "reason", "created_at", "updated_at",
)
BUTTON_UPDATABLE = frozenset({"state", "px_kind", "action", "ticket_ref", "ticket_url", "reason"})


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

    def recent_text_route(
        self, watch_id: str, text_hash: str, received_epoch: float, detection_id: str, window_s: float,
        conv_key: str, twin_providers: frozenset[str] = frozenset(),
    ) -> str | None:
        """A detection of the same target and text on a different twin provider (sms vs rcs), already acted
        on inside the window. Same-provider repeats are the matcher's 2 s rule; a failed or skipped route
        never counts, because it did nothing for the message."""
        own = conv_key.split(":", 1)[0]
        with self.connect() as db:
            rows = db.execute(
                "SELECT detection_id, conv_key FROM routes WHERE watch_id=? AND text_hash=? AND detection_id<>? "
                "AND abs(received_epoch-?)<=? AND action IS NOT NULL AND action NOT IN ('skipped_duplicate','failed')",
                (watch_id, text_hash, detection_id, received_epoch, window_s),
            ).fetchall()
        for row_id, row_conv in rows:
            other = row_conv.split(":", 1)[0]
            if own in twin_providers and other in twin_providers and other != own:
                return row_id
        return None

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

    # --- ring throttle --------------------------------------------------------

    def last_ring(self, watch_id: str, channel: str) -> float | None:
        """Epoch of the last ring placed on this (watch, channel); None when never, or on a state file
        written before the table existed (a read-only dry run cannot create it)."""
        try:
            with self.connect() as db:
                row = db.execute("SELECT last_epoch FROM rings WHERE watch_id=? AND channel=?", (watch_id, channel)).fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None

    def record_ring(self, watch_id: str, channel: str, epoch: float) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO rings VALUES(?,?,?)", (watch_id, channel, epoch))

    # --- notification log -----------------------------------------------------------
    # One row per channel attempt of a detection (a busy-phone retry is attempt 2), so "did it reach
    # him" has a durable answer. `channel` is the masked form; `ref` is a provider id worth looking up
    # (the Telnyx call_leg_id of a ring). Additive: the table is created beside the old ones.

    def record_notification(self, detection_id: str, watch_id: str, channel: str, outcome: str, detail: str | None = None,
                            *, attempt: int = 1, ref: str | None = None, at: float | None = None) -> int:
        now = at if at is not None else time.time()
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO notifications(detection_id,watch_id,channel,attempt,outcome,detail,ref,at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (detection_id, watch_id, channel, attempt, outcome, detail, ref, now, now),
            )
            return int(cursor.lastrowid)

    def update_notification(self, row_id: int, outcome: str, detail: str | None = None, *, at: float | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE notifications SET outcome=?, detail=?, updated_at=? WHERE id=?",
                       (outcome, detail, at if at is not None else time.time(), row_id))

    # --- the Create Ticket button -----------------------------------------------------
    # A detection routed `awaiting_button` files nothing; the whole detection envelope is kept here, because
    # the tap must file exactly what auto mode would have filed (px needs the sender, the text, the
    # provider and the conversation, none of which the routed event carries). `state` is the tap's own
    # ledger, with the same no-blind-retry rule as a route: awaiting -> pending (written before px runs)
    # -> done | failed. A `pending` row found when no px is running means the sink stopped mid-run, and
    # it becomes `failed`, never a second create. `channels` are the ntfy channels the push went to (the
    # follow-up goes there). Additive: created beside the old tables.

    def open_button(self, detection_id: str, watch_id: str, detection: dict, channels) -> None:
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO buttons(detection_id,watch_id,detection,channels,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (detection_id, watch_id, json.dumps(detection, sort_keys=True), json.dumps(list(channels)), now, now),
            )

    def button(self, detection_id: str) -> dict | None:
        """The stored button, or None (also on a state file written before the table existed)."""
        try:
            with self.connect() as db:
                row = db.execute(f"SELECT {','.join(BUTTON_FIELDS)} FROM buttons WHERE detection_id=?", (detection_id,)).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        out = dict(zip(BUTTON_FIELDS, row))
        out["detection"] = json.loads(out["detection"])
        out["channels"] = json.loads(out["channels"] or "[]")
        return out

    def update_button(self, detection_id: str, **fields) -> None:
        for key in fields:
            if key not in BUTTON_UPDATABLE:
                raise KeyError(key)
        if not fields:
            return
        with self.connect() as db:
            db.execute(
                "UPDATE buttons SET " + ",".join(f"{k}=?" for k in fields) + ",updated_at=? WHERE detection_id=?",
                (*fields.values(), time.time(), detection_id),
            )

    def recent_routes(self, limit: int, watch_id: str | None = None) -> list[dict]:
        """The sink's ledger, newest message first, each with its notification rows (oldest first).
        A state file written before the notification log existed reads as having no rows."""
        sql = f"SELECT {','.join(ROUTE_FIELDS)} FROM routes"
        args: list = []
        if watch_id:
            sql += " WHERE watch_id=?"
            args.append(watch_id)
        sql += " ORDER BY received_epoch DESC, created_at DESC LIMIT ?"
        args.append(max(1, limit))
        with self.connect() as db:
            rows = db.execute(sql, args).fetchall()
            out = []
            for row in rows:
                route = dict(zip(ROUTE_FIELDS, row))
                route["notify_channels"] = json.loads(route["notify_channels"] or "[]")
                route["envelope"] = None  # the status view never needs the stored event
                try:
                    notes = db.execute(
                        "SELECT channel,attempt,outcome,detail,ref,at,updated_at FROM notifications WHERE detection_id=? ORDER BY id",
                        (route["detection_id"],),
                    ).fetchall()
                except sqlite3.OperationalError:
                    notes = []
                route["notifications"] = [
                    dict(zip(("channel", "attempt", "outcome", "detail", "ref", "at", "updated_at"), note)) for note in notes
                ]
                try:
                    button = db.execute("SELECT state,action,ticket_ref FROM buttons WHERE detection_id=?",
                                        (route["detection_id"],)).fetchone()
                except sqlite3.OperationalError:
                    button = None
                route["button"] = dict(zip(("state", "action", "ticket_ref"), button)) if button else None
                out.append(route)
        return out
