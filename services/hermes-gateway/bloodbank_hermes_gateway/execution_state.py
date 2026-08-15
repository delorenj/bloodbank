"""Durable execution journal for Bloodbank command delivery.

JetStream is at-least-once. This journal does not claim exactly-once transport;
it records the local execution boundary so a completed Hermes command can be
replayed through terminal publication and acknowledgement without executing
the command again.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def envelope_digest(envelope: dict[str, Any]) -> str:
    payload = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _events_json(events: tuple[dict[str, Any], ...]) -> str:
    return json.dumps(events, separators=(",", ":"), sort_keys=True)


def _decode_events(value: str | None) -> tuple[dict[str, Any], ...]:
    if not value:
        return ()
    decoded = json.loads(value)
    if not isinstance(decoded, list) or any(
        not isinstance(item, dict) for item in decoded
    ):
        raise RuntimeError("execution journal contains invalid lifecycle events")
    return tuple(decoded)


@dataclass(frozen=True)
class ExecutionRecord:
    command_id: str
    envelope_digest: str
    profile: str
    state: str
    outcome: str | None
    started_events: tuple[dict[str, Any], ...]
    terminal_events: tuple[dict[str, Any], ...]
    rejection_reason: str | None


class ExecutionStateStore:
    """SQLite-backed pending/completed/rejected state with atomic transitions."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        if self.path.is_symlink():
            raise ValueError(f"execution_state_file must not be a symlink: {self.path}")
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.parent.is_symlink():
            raise ValueError(
                f"execution_state_file parent must not be a symlink: {self.path.parent}"
            )
        if not parent_existed:
            os.chmod(self.path.parent, 0o700)
        with self._connect() as db:
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(executions)").fetchall()
            }
            if not columns:
                self._create_schema(db)
            elif "rejection_reason" not in columns:
                self._migrate_pending_completed_schema(db)
        os.chmod(self.path, 0o600)

    @staticmethod
    def _create_schema(db: sqlite3.Connection) -> None:
        db.execute(
            """
            CREATE TABLE executions (
                command_id TEXT PRIMARY KEY,
                envelope_digest TEXT NOT NULL,
                profile TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'completed', 'rejected')),
                outcome TEXT CHECK (outcome IN ('success', 'failure', 'cancelled')),
                started_events TEXT NOT NULL,
                terminal_events TEXT,
                rejection_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (
                    (
                        state = 'pending' AND outcome IS NULL
                        AND terminal_events IS NULL AND rejection_reason IS NULL
                    )
                    OR
                    (
                        state = 'completed' AND outcome IS NOT NULL
                        AND terminal_events IS NOT NULL AND rejection_reason IS NULL
                    )
                    OR
                    (
                        state = 'rejected' AND outcome IS NULL
                        AND terminal_events IS NULL AND rejection_reason IS NOT NULL
                    )
                )
            )
            """
        )
        db.execute("PRAGMA user_version = 2")

    @classmethod
    def _migrate_pending_completed_schema(cls, db: sqlite3.Connection) -> None:
        """Upgrade the original journal without losing pending/completed evidence."""

        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("ALTER TABLE executions RENAME TO executions_v1")
            cls._create_schema(db)
            db.execute(
                """
                INSERT INTO executions(
                    command_id, envelope_digest, profile, state, outcome,
                    started_events, terminal_events, rejection_reason,
                    created_at, updated_at
                )
                SELECT command_id, envelope_digest, profile, state, outcome,
                       started_events, terminal_events, NULL, created_at, updated_at
                FROM executions_v1
                """
            )
            db.execute("DROP TABLE executions_v1")
            db.commit()
        except Exception:
            db.rollback()
            raise

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30.0)
        db.execute("PRAGMA busy_timeout = 30000")
        db.execute("PRAGMA synchronous = FULL")
        return db

    @staticmethod
    def _record(row: tuple[Any, ...] | None) -> ExecutionRecord | None:
        if row is None:
            return None
        return ExecutionRecord(
            command_id=str(row[0]),
            envelope_digest=str(row[1]),
            profile=str(row[2]),
            state=str(row[3]),
            outcome=str(row[4]) if row[4] is not None else None,
            started_events=_decode_events(row[5]),
            terminal_events=_decode_events(row[6]),
            rejection_reason=str(row[7]) if row[7] is not None else None,
        )

    def get(self, command_id: str) -> ExecutionRecord | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT command_id, envelope_digest, profile, state, outcome,
                          started_events, terminal_events, rejection_reason
                   FROM executions WHERE command_id = ?""",
                (command_id,),
            ).fetchone()
        return self._record(row)

    def claim_pending(
        self,
        *,
        command_id: str,
        digest: str,
        profile: str,
        started_events: tuple[dict[str, Any], ...],
    ) -> ExecutionRecord:
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT OR IGNORE INTO executions
                   (command_id, envelope_digest, profile, state, outcome,
                    started_events, terminal_events, created_at, updated_at)
                   VALUES (?, ?, ?, 'pending', NULL, ?, NULL, ?, ?)""",
                (command_id, digest, profile, _events_json(started_events), now, now),
            )
            row = db.execute(
                """SELECT command_id, envelope_digest, profile, state, outcome,
                          started_events, terminal_events, rejection_reason
                   FROM executions WHERE command_id = ?""",
                (command_id,),
            ).fetchone()
            db.commit()
        record = self._record(row)
        if record is None:
            raise RuntimeError("execution journal failed to persist pending command")
        return record

    def mark_completed(
        self,
        *,
        command_id: str,
        digest: str,
        outcome: str,
        terminal_events: tuple[dict[str, Any], ...],
    ) -> ExecutionRecord:
        if outcome not in {"success", "failure", "cancelled"}:
            raise ValueError(f"unsupported execution outcome: {outcome}")
        now = _now()
        encoded_terminal = _events_json(terminal_events)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT command_id, envelope_digest, profile, state, outcome,
                          started_events, terminal_events, rejection_reason
                   FROM executions WHERE command_id = ?""",
                (command_id,),
            ).fetchone()
            current = self._record(row)
            if current is None:
                db.rollback()
                raise RuntimeError(
                    "execution journal has no pending command to complete"
                )
            if current.envelope_digest != digest:
                db.rollback()
                raise RuntimeError(
                    "command_id collides with a different command envelope"
                )
            if current.state == "completed":
                if (
                    current.outcome != outcome
                    or current.terminal_events != terminal_events
                ):
                    db.rollback()
                    raise RuntimeError(
                        "completed execution journal result is immutable"
                    )
                db.commit()
                return current
            if current.state == "rejected":
                db.rollback()
                raise RuntimeError("rejected execution journal result is immutable")
            db.execute(
                """UPDATE executions
                   SET state = 'completed', outcome = ?, terminal_events = ?, updated_at = ?
                   WHERE command_id = ? AND state = 'pending'""",
                (outcome, encoded_terminal, now, command_id),
            )
            row = db.execute(
                """SELECT command_id, envelope_digest, profile, state, outcome,
                          started_events, terminal_events, rejection_reason
                   FROM executions WHERE command_id = ?""",
                (command_id,),
            ).fetchone()
            db.commit()
        record = self._record(row)
        if record is None or record.state != "completed":
            raise RuntimeError("execution journal failed to persist completed command")
        return record

    def mark_rejected(
        self,
        *,
        command_id: str,
        digest: str,
        reason: str,
    ) -> ExecutionRecord:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("execution rejection reason must be non-empty")
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT command_id, envelope_digest, profile, state, outcome,
                          started_events, terminal_events, rejection_reason
                   FROM executions WHERE command_id = ?""",
                (command_id,),
            ).fetchone()
            current = self._record(row)
            if current is None:
                db.rollback()
                raise RuntimeError("execution journal has no pending command to reject")
            if current.envelope_digest != digest:
                db.rollback()
                raise RuntimeError(
                    "command_id collides with a different command envelope"
                )
            if current.state == "completed":
                db.rollback()
                raise RuntimeError("completed execution journal result is immutable")
            if current.state == "rejected":
                if current.rejection_reason != normalized_reason:
                    db.rollback()
                    raise RuntimeError("rejected execution journal result is immutable")
                db.commit()
                return current
            db.execute(
                """UPDATE executions
                   SET state = 'rejected', rejection_reason = ?, updated_at = ?
                   WHERE command_id = ? AND state = 'pending'""",
                (normalized_reason, now, command_id),
            )
            row = db.execute(
                """SELECT command_id, envelope_digest, profile, state, outcome,
                          started_events, terminal_events, rejection_reason
                   FROM executions WHERE command_id = ?""",
                (command_id,),
            ).fetchone()
            db.commit()
        record = self._record(row)
        if record is None or record.state != "rejected":
            raise RuntimeError("execution journal failed to persist rejected command")
        return record
