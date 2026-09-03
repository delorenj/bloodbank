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


def execution_semantic_digest(envelope: dict[str, Any]) -> str:
    """Hash executable intent while excluding broker delivery identities."""
    semantic = {
        key: value
        for key, value in envelope.items()
        if key not in {"id", "command_id", "time", "correlationid", "causationid"}
    }
    payload = json.dumps(semantic, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


class ExecutionSemanticCollision(RuntimeError):
    """A target/idempotency identity was reused for different intent."""


class ExecutionEnvelopeCollision(RuntimeError):
    """A command identity was reused for a different exact envelope."""


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
    semantic_digest: str
    target_agent_id: str
    idempotency_key: str
    profile: str
    state: str
    outcome: str | None
    started_events: tuple[dict[str, Any], ...]
    terminal_events: tuple[dict[str, Any], ...]
    rejection_reason: str | None
    rejected_at: str | None


class ExecutionStateStore:
    """SQLite-backed command state with crash-safe lifecycle transitions."""

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
            else:
                version = int(db.execute("PRAGMA user_version").fetchone()[0])
                if version > 4:
                    raise RuntimeError(
                        f"execution journal schema version {version} is unsupported"
                    )
                required_columns = {
                    "rejected_at",
                    "semantic_digest",
                    "target_agent_id",
                    "idempotency_key",
                }
                if version != 4 or not required_columns.issubset(columns):
                    self._migrate_to_current_schema(db, columns, version=version)
        os.chmod(self.path, 0o600)

    @staticmethod
    def _create_schema(db: sqlite3.Connection) -> None:
        db.execute(
            """
            CREATE TABLE executions (
                command_id TEXT PRIMARY KEY,
                envelope_digest TEXT NOT NULL,
                semantic_digest TEXT NOT NULL,
                target_agent_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                profile TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'pending', 'started', 'completed',
                        'rejected_pre_start', 'rejected_closing', 'rejected_closed'
                    )
                ),
                outcome TEXT CHECK (outcome IN ('success', 'failure', 'cancelled')),
                started_events TEXT NOT NULL,
                terminal_events TEXT,
                rejection_reason TEXT,
                rejected_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (
                    (
                        state = 'pending' AND outcome IS NULL
                        AND terminal_events IS NULL AND rejection_reason IS NULL
                        AND rejected_at IS NULL
                    )
                    OR
                    (
                        state = 'started' AND outcome IS NULL
                        AND terminal_events IS NULL AND rejection_reason IS NULL
                        AND rejected_at IS NULL
                    )
                    OR
                    (
                        state = 'completed' AND outcome IS NOT NULL
                        AND terminal_events IS NOT NULL AND rejection_reason IS NULL
                        AND rejected_at IS NULL
                    )
                    OR
                    (
                        state = 'rejected_pre_start' AND outcome IS NULL
                        AND terminal_events IS NULL AND rejection_reason IS NOT NULL
                        AND rejected_at IS NOT NULL
                    )
                    OR
                    (
                        state = 'rejected_closing' AND outcome IS NULL
                        AND terminal_events IS NULL AND rejection_reason IS NOT NULL
                        AND rejected_at IS NOT NULL
                    )
                    OR
                    (
                        state = 'rejected_closed' AND outcome IS NULL
                        AND terminal_events IS NOT NULL AND rejection_reason IS NOT NULL
                        AND rejected_at IS NOT NULL
                    )
                ),
                UNIQUE (target_agent_id, idempotency_key)
            )
            """
        )
        db.execute("PRAGMA user_version = 4")

    @classmethod
    def _migrate_to_current_schema(
        cls,
        db: sqlite3.Connection,
        columns: set[str],
        *,
        version: int,
    ) -> None:
        """Upgrade legacy journals while preserving every lifecycle fact."""

        db.execute("BEGIN IMMEDIATE")
        try:
            rejection_column = "rejection_reason" if "rejection_reason" in columns else "NULL"
            rejected_at_column = "rejected_at" if "rejected_at" in columns else "NULL"
            semantic_column = "semantic_digest" if "semantic_digest" in columns else "NULL"
            target_column = "target_agent_id" if "target_agent_id" in columns else "NULL"
            idempotency_column = "idempotency_key" if "idempotency_key" in columns else "NULL"
            rows = db.execute(
                f"""SELECT command_id, envelope_digest, profile, state, outcome,
                           started_events, terminal_events, {rejection_column},
                           {rejected_at_column}, created_at, updated_at,
                           {semantic_column}, {target_column}, {idempotency_column}
                    FROM executions"""  # noqa: S608 - columns come from constants
            ).fetchall()
            db.execute("ALTER TABLE executions RENAME TO executions_legacy")
            cls._create_schema(db)
            migrated_at = _now()
            for row in rows:
                old_state = str(row[3])
                if old_state == "completed":
                    state = "completed"
                    outcome = row[4]
                    terminal = row[6]
                    rejection_reason = None
                    rejected_at = None
                elif old_state == "rejected":
                    # v2 could not distinguish a pre-start rejection from one
                    # after lifecycle publication. Preserve the terminal policy
                    # decision and conservatively require closure on redelivery.
                    state = "rejected_closing"
                    outcome = None
                    terminal = None
                    rejection_reason = row[7] or "route_policy_invalid_before_dispatch"
                    rejected_at = migrated_at
                elif old_state == "pending":
                    # v1/v2 published starts without a durable phase marker;
                    # v3 made pending a proven pre-publication state.
                    state = "pending" if version >= 3 else "started"
                    outcome = None
                    terminal = None
                    rejection_reason = None
                    rejected_at = None
                elif old_state == "started":
                    state = "started"
                    outcome = None
                    terminal = None
                    rejection_reason = None
                    rejected_at = None
                elif old_state == "rejected_pre_start":
                    state = old_state
                    outcome = None
                    terminal = None
                    rejection_reason = row[7]
                    rejected_at = row[8] or migrated_at
                elif old_state == "rejected_closing":
                    state = old_state
                    outcome = None
                    terminal = None
                    rejection_reason = row[7]
                    rejected_at = row[8] or migrated_at
                elif old_state == "rejected_closed":
                    state = old_state
                    outcome = None
                    terminal = row[6]
                    rejection_reason = row[7]
                    rejected_at = row[8] or migrated_at
                else:
                    raise RuntimeError(
                        f"execution journal contains unsupported state {old_state!r}"
                    )
                legacy_identity = f"legacy:{row[0]}"
                db.execute(
                    """INSERT INTO executions(
                           command_id, envelope_digest, semantic_digest,
                           target_agent_id, idempotency_key, profile, state,
                           outcome, started_events, terminal_events,
                           rejection_reason, rejected_at, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row[0],
                        row[1],
                        row[11] or row[1],
                        row[12] or legacy_identity,
                        row[13] or legacy_identity,
                        row[2],
                        state,
                        outcome,
                        row[5],
                        terminal,
                        rejection_reason,
                        rejected_at,
                        row[9],
                        row[10],
                    ),
                )
            db.execute("DROP TABLE executions_legacy")
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
            semantic_digest=str(row[2]),
            target_agent_id=str(row[3]),
            idempotency_key=str(row[4]),
            profile=str(row[5]),
            state=str(row[6]),
            outcome=str(row[7]) if row[7] is not None else None,
            started_events=_decode_events(row[8]),
            terminal_events=_decode_events(row[9]),
            rejection_reason=str(row[10]) if row[10] is not None else None,
            rejected_at=str(row[11]) if row[11] is not None else None,
        )

    @classmethod
    def _select(cls, db: sqlite3.Connection, command_id: str) -> ExecutionRecord | None:
        row = db.execute(
            """SELECT command_id, envelope_digest, semantic_digest,
                      target_agent_id, idempotency_key, profile, state, outcome,
                      started_events, terminal_events, rejection_reason, rejected_at
               FROM executions WHERE command_id = ?""",
            (command_id,),
        ).fetchone()
        return cls._record(row)

    @classmethod
    def _select_identity(
        cls,
        db: sqlite3.Connection,
        target_agent_id: str,
        idempotency_key: str,
    ) -> ExecutionRecord | None:
        row = db.execute(
            """SELECT command_id, envelope_digest, semantic_digest,
                      target_agent_id, idempotency_key, profile, state, outcome,
                      started_events, terminal_events, rejection_reason, rejected_at
               FROM executions
               WHERE target_agent_id = ? AND idempotency_key = ?""",
            (target_agent_id, idempotency_key),
        ).fetchone()
        return cls._record(row)

    @staticmethod
    def _require_digest(current: ExecutionRecord, digest: str) -> None:
        if current.envelope_digest != digest:
            raise ExecutionEnvelopeCollision(
                "command_id collides with a different command envelope"
            )

    @staticmethod
    def _reason(reason: str) -> str:
        normalized = reason.strip()
        if not normalized:
            raise ValueError("execution rejection reason must be non-empty")
        return normalized

    def get(self, command_id: str) -> ExecutionRecord | None:
        with self._connect() as db:
            return self._select(db, command_id)

    def get_by_identity(
        self,
        target_agent_id: str,
        idempotency_key: str,
    ) -> ExecutionRecord | None:
        with self._connect() as db:
            return self._select_identity(db, target_agent_id, idempotency_key)

    def claim_pending(
        self,
        *,
        command_id: str,
        digest: str,
        semantic_digest: str | None = None,
        target_agent_id: str | None = None,
        idempotency_key: str | None = None,
        profile: str,
        started_events: tuple[dict[str, Any], ...],
    ) -> ExecutionRecord:
        semantic = semantic_digest or digest
        target = target_agent_id or f"legacy:{command_id}"
        idempotency = idempotency_key or f"legacy:{command_id}"
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                record = self._select(db, command_id)
                if record is not None:
                    self._require_digest(record, digest)
                    db.commit()
                    return record
                record = self._select_identity(db, target, idempotency)
                if record is not None:
                    if record.semantic_digest != semantic:
                        raise ExecutionSemanticCollision(
                            "target_agent_id and idempotency_key collide with "
                            "different execution intent"
                        )
                    db.commit()
                    return record
                db.execute(
                    """INSERT INTO executions
                       (command_id, envelope_digest, semantic_digest,
                        target_agent_id, idempotency_key, profile, state, outcome,
                        started_events, terminal_events, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL, ?, ?)""",
                    (
                        command_id,
                        digest,
                        semantic,
                        target,
                        idempotency,
                        profile,
                        _events_json(started_events),
                        now,
                        now,
                    ),
                )
                record = self._select(db, command_id)
                db.commit()
            except Exception:
                db.rollback()
                raise
        if record is None:
            raise RuntimeError("execution journal failed to persist pending command")
        return record

    def mark_started(self, *, command_id: str, digest: str) -> ExecutionRecord:
        """Record that started-event publication may begin before publishing."""

        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._select(db, command_id)
            if current is None:
                db.rollback()
                raise RuntimeError("execution journal has no pending command to start")
            try:
                self._require_digest(current, digest)
            except RuntimeError:
                db.rollback()
                raise
            if current.state == "started":
                db.commit()
                return current
            if current.state != "pending":
                db.rollback()
                raise RuntimeError("finalized execution journal state is immutable")
            db.execute(
                """UPDATE executions SET state = 'started', updated_at = ?
                   WHERE command_id = ? AND state = 'pending'""",
                (now, command_id),
            )
            record = self._select(db, command_id)
            db.commit()
        if record is None or record.state != "started":
            raise RuntimeError("execution journal failed to persist started command")
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
            current = self._select(db, command_id)
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
            if current.state != "started":
                db.rollback()
                raise RuntimeError("execution must be started before completion")
            db.execute(
                """UPDATE executions
                   SET state = 'completed', outcome = ?, terminal_events = ?, updated_at = ?
                   WHERE command_id = ? AND state = 'started'""",
                (outcome, encoded_terminal, now, command_id),
            )
            record = self._select(db, command_id)
            db.commit()
        if record is None or record.state != "completed":
            raise RuntimeError("execution journal failed to persist completed command")
        return record

    def reject_pre_start(
        self,
        *,
        command_id: str,
        digest: str,
        reason: str,
    ) -> ExecutionRecord:
        """Persist an eventless rejection before any start publish was attempted."""

        normalized_reason = self._reason(reason)
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._select(db, command_id)
            if current is None:
                db.rollback()
                raise RuntimeError("execution journal has no pending command to reject")
            try:
                self._require_digest(current, digest)
            except RuntimeError:
                db.rollback()
                raise
            if current.state == "rejected_pre_start":
                if current.rejection_reason != normalized_reason:
                    db.rollback()
                    raise RuntimeError("rejected execution journal result is immutable")
                db.commit()
                return current
            if current.state != "pending":
                db.rollback()
                raise RuntimeError(
                    "started execution requires terminal rejection closure"
                )
            db.execute(
                """UPDATE executions
                   SET state = 'rejected_pre_start', rejection_reason = ?,
                       rejected_at = ?, updated_at = ?
                   WHERE command_id = ? AND state = 'pending'""",
                (normalized_reason, now, now, command_id),
            )
            record = self._select(db, command_id)
            db.commit()
        if record is None or record.state != "rejected_pre_start":
            raise RuntimeError(
                "execution journal failed to persist pre-start rejection"
            )
        return record

    def begin_rejection(
        self,
        *,
        command_id: str,
        digest: str,
        reason: str,
    ) -> ExecutionRecord:
        """Persist immutable rejection intent for a lifecycle that may be open."""

        normalized_reason = self._reason(reason)
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._select(db, command_id)
            if current is None:
                db.rollback()
                raise RuntimeError("execution journal has no started command to reject")
            try:
                self._require_digest(current, digest)
            except RuntimeError:
                db.rollback()
                raise
            if current.state in {"rejected_closing", "rejected_closed"}:
                if current.rejection_reason != normalized_reason:
                    db.rollback()
                    raise RuntimeError("rejected execution journal result is immutable")
                db.commit()
                return current
            if current.state != "started":
                db.rollback()
                raise RuntimeError(
                    "only a started execution can begin terminal rejection"
                )
            db.execute(
                """UPDATE executions
                   SET state = 'rejected_closing', rejection_reason = ?,
                       rejected_at = ?, updated_at = ?
                   WHERE command_id = ? AND state = 'started'""",
                (normalized_reason, now, now, command_id),
            )
            record = self._select(db, command_id)
            db.commit()
        if record is None or record.state != "rejected_closing":
            raise RuntimeError("execution journal failed to persist rejection intent")
        return record

    def finalize_rejection(
        self,
        *,
        command_id: str,
        digest: str,
        terminal_events: tuple[dict[str, Any], ...],
    ) -> ExecutionRecord:
        """Atomically store the exact terminal closure before it is published."""

        if len(terminal_events) != 2:
            raise ValueError(
                "terminal rejection closure must contain exactly two events"
            )
        encoded_terminal = _events_json(terminal_events)
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._select(db, command_id)
            if current is None:
                db.rollback()
                raise RuntimeError("execution journal has no rejection intent to close")
            try:
                self._require_digest(current, digest)
            except RuntimeError:
                db.rollback()
                raise
            if current.state == "rejected_closed":
                if current.terminal_events != terminal_events:
                    db.rollback()
                    raise RuntimeError("rejected execution journal result is immutable")
                db.commit()
                return current
            if current.state != "rejected_closing":
                db.rollback()
                raise RuntimeError(
                    "execution rejection intent is not ready for closure"
                )
            db.execute(
                """UPDATE executions
                   SET state = 'rejected_closed', terminal_events = ?, updated_at = ?
                   WHERE command_id = ? AND state = 'rejected_closing'""",
                (encoded_terminal, now, command_id),
            )
            record = self._select(db, command_id)
            db.commit()
        if record is None or record.state != "rejected_closed":
            raise RuntimeError("execution journal failed to persist rejection closure")
        return record
