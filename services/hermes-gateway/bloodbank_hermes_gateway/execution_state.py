"""Durable execution journal for Bloodbank command delivery.

JetStream is at-least-once. This journal does not claim exactly-once transport.
Native model finalization and later platform processing are recorded separately,
so capture survives a crash without declaring success early. Publishers replay
the resolved immutable outbox without executing Hermes.
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
    rejected_at: str | None
    # Delivery lane the command arrived on ('batch' | 'interactive'); None for
    # rows written before the interactive lane existed.
    lane: str | None = None
    # Machine reason a 'failure' outcome was closed with, e.g. 'turn_timeout'
    # when the lane's per-turn wall clock expired; None for ordinary outcomes
    # and for rows written before the column existed.
    failure_code: str | None = None
    # UTC time a turn of this command outlived its lane's wall clock and
    # detached (gave its slot back and ran on); None when it never detached and
    # for rows written before the column existed.
    detached_at: str | None = None
    # Native model finalization is durable before platform processing settles.
    # A started row with capture_outcome set must never execute Hermes again.
    capture_outcome: str | None = None
    captured_events: tuple[dict[str, Any], ...] = ()
    capture_error: str | None = None
    # The bloodbank.agent.invocation.queued fact stored at claim and replayed
    # with the same id; empty for rows claimed before the column existed (their
    # lifecycle opened at started, so nothing closes a queued).
    queued_events: tuple[dict[str, Any], ...] = ()


class ExecutionStateStore:
    """SQLite command journal and ordered result outbox.

    Native output is captured separately from final platform processing.
    terminal_events is the resolved immutable publishable batch; old records
    retain their original meaning and bytes.
    """

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
            if columns:
                version = int(db.execute("PRAGMA user_version").fetchone()[0])
                if version > 5:
                    raise RuntimeError(
                        f"execution journal schema version {version} is unsupported"
                    )
                if version < 3 or "rejected_at" not in columns:
                    self._migrate_to_current_schema(db, columns)
            else:
                self._create_schema(db)
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(executions)").fetchall()
            }
            # Nullable additions preserve every historical row and outbox byte.
            for column in (
                "lane", "failure_code", "detached_at", "capture_outcome",
                "captured_events", "capture_error", "queued_events",
            ):
                if column not in columns:
                    db.execute(f"ALTER TABLE executions ADD COLUMN {column} TEXT")
            # Older gateways must refuse this journal: v4 added capture state (a
            # captured turn must never rerun); v5 builds started facts at the
            # real start, so a pending row no longer carries them.
            db.execute("PRAGMA user_version = 5")
        os.chmod(self.path, 0o600)

    @staticmethod
    def _create_schema(db: sqlite3.Connection) -> None:
        db.execute(
            """
            CREATE TABLE executions (
                command_id TEXT PRIMARY KEY,
                envelope_digest TEXT NOT NULL,
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
                lane TEXT,
                failure_code TEXT,
                detached_at TEXT,
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
                )
            )
            """
        )
        db.execute("PRAGMA user_version = 3")

    @classmethod
    def _migrate_to_current_schema(
        cls, db: sqlite3.Connection, columns: set[str]
    ) -> None:
        """Upgrade v1/v2 journals without assuming pending starts never escaped."""

        db.execute("BEGIN IMMEDIATE")
        try:
            rejection_column = (
                "rejection_reason" if "rejection_reason" in columns else "NULL"
            )
            rows = db.execute(
                f"""SELECT command_id, envelope_digest, profile, state, outcome,
                           started_events, terminal_events, {rejection_column},
                           created_at, updated_at
                    FROM executions"""  # noqa: S608 - column is selected from a constant
            ).fetchall()
            db.execute("ALTER TABLE executions RENAME TO executions_v1")
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
                    # v1/v2 published starts without a durable phase marker.
                    # Treat them as possibly escaped so a later rejection closes
                    # the lifecycle instead of recreating the historical orphan.
                    state = "started"
                    outcome = None
                    terminal = None
                    rejection_reason = None
                    rejected_at = None
                else:
                    raise RuntimeError(
                        f"execution journal contains unsupported state {old_state!r}"
                    )
                db.execute(
                    """INSERT INTO executions(
                           command_id, envelope_digest, profile, state, outcome,
                           started_events, terminal_events, rejection_reason,
                           rejected_at, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row[0],
                        row[1],
                        row[2],
                        state,
                        outcome,
                        row[5],
                        terminal,
                        rejection_reason,
                        rejected_at,
                        row[8],
                        row[9],
                    ),
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
            rejected_at=str(row[8]) if row[8] is not None else None,
            lane=str(row[9]) if row[9] is not None else None,
            failure_code=str(row[10]) if row[10] is not None else None,
            detached_at=str(row[11]) if row[11] is not None else None,
            capture_outcome=str(row[12]) if row[12] is not None else None,
            captured_events=_decode_events(row[13]),
            capture_error=str(row[14]) if row[14] is not None else None,
            queued_events=_decode_events(row[15]),
        )

    @classmethod
    def _select(cls, db: sqlite3.Connection, command_id: str) -> ExecutionRecord | None:
        row = db.execute(
            """SELECT command_id, envelope_digest, profile, state, outcome,
                      started_events, terminal_events, rejection_reason, rejected_at,
                      lane, failure_code, detached_at, capture_outcome,
                      captured_events, capture_error, queued_events
               FROM executions WHERE command_id = ?""",
            (command_id,),
        ).fetchone()
        return cls._record(row)

    @staticmethod
    def _require_digest(current: ExecutionRecord, digest: str) -> None:
        if current.envelope_digest != digest:
            raise RuntimeError("command_id collides with a different command envelope")

    @staticmethod
    def _reason(reason: str) -> str:
        normalized = reason.strip()
        if not normalized:
            raise ValueError("execution rejection reason must be non-empty")
        return normalized

    def get(self, command_id: str) -> ExecutionRecord | None:
        with self._connect() as db:
            return self._select(db, command_id)

    def claim_pending(
        self,
        *,
        command_id: str,
        digest: str,
        profile: str,
        started_events: tuple[dict[str, Any], ...] = (),
        queued_events: tuple[dict[str, Any], ...] = (),
        lane: str | None = None,
    ) -> ExecutionRecord:
        """Durably claim a command as pending, with its queued fact.

        ``started_events`` is optional and normally empty: ``mark_started``
        builds them so ``invocation.started.time`` is the real start."""

        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT OR IGNORE INTO executions
                   (command_id, envelope_digest, profile, state, outcome,
                    started_events, terminal_events, lane, queued_events,
                    created_at, updated_at)
                   VALUES (?, ?, ?, 'pending', NULL, ?, NULL, ?, ?, ?, ?)""",
                (
                    command_id,
                    digest,
                    profile,
                    _events_json(started_events),
                    lane,
                    _events_json(queued_events) if queued_events else None,
                    now,
                    now,
                ),
            )
            record = self._select(db, command_id)
            db.commit()
        if record is None:
            raise RuntimeError("execution journal failed to persist pending command")
        return record

    def set_queued_events(
        self,
        *,
        command_id: str,
        digest: str,
        queued_events: tuple[dict[str, Any], ...],
    ) -> ExecutionRecord:
        """Backfill the queued fact on a row claimed before the column existed.

        Only a still-pending row with none stored takes it (a started row opened
        its lifecycle without one, so nothing would close it). Idempotent: a row
        that already has queued events is returned unchanged."""

        if not queued_events:
            raise ValueError("queued events must not be empty")
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._select(db, command_id)
            if current is None:
                db.rollback()
                raise RuntimeError("execution journal has no pending command to queue")
            try:
                self._require_digest(current, digest)
            except RuntimeError:
                db.rollback()
                raise
            if current.queued_events or current.state != "pending":
                db.commit()
                return current
            db.execute(
                """UPDATE executions SET queued_events = ?, updated_at = ?
                   WHERE command_id = ? AND state = 'pending'
                     AND queued_events IS NULL""",
                (_events_json(queued_events), now, command_id),
            )
            record = self._select(db, command_id)
            db.commit()
        if record is None:
            raise RuntimeError("execution journal lost a pending command")
        return record

    def mark_started(
        self,
        *,
        command_id: str,
        digest: str,
        started_events: tuple[dict[str, Any], ...] | None = None,
    ) -> ExecutionRecord:
        """Record that started-event publication may begin before publishing.

        ``started_events``, when given, replaces whatever a pending row holds
        (nothing was ever published for a pending row), so the started facts'
        ``time`` is the real start, not the claim. A row that is already
        'started' keeps the facts it stored.

        On a row that is already 'started' this is a new attempt of the same
        command (a NAK or an unclean exit cut the last one off): the last
        attempt's ``detached_at`` is cleared, since it says nothing about this
        one."""

        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._select(db, command_id)
            if current is None:
                db.rollback()
                raise RuntimeError("execution journal has no pending command to start")
            self._require_digest(current, digest)
            if current.capture_outcome is not None:
                raise RuntimeError("captured execution cannot be dispatched again")
            if current.state == "started":
                if current.detached_at is None:
                    db.commit()
                    return current
                db.execute(
                    """UPDATE executions SET detached_at = NULL, updated_at = ?
                       WHERE command_id = ? AND state = 'started'""",
                    (now, command_id),
                )
                record = self._select(db, command_id)
                db.commit()
                if record is None:
                    raise RuntimeError("execution journal lost a started command")
                return record
            if current.state != "pending":
                db.rollback()
                raise RuntimeError("finalized execution journal state is immutable")
            if started_events:
                db.execute(
                    """UPDATE executions
                       SET state = 'started', started_events = ?, updated_at = ?
                       WHERE command_id = ? AND state = 'pending'""",
                    (_events_json(started_events), now, command_id),
                )
            else:
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

    def mark_detached(self, *, command_id: str, digest: str) -> ExecutionRecord:
        """Stamp ``detached_at`` when a turn runs past its lane's wall clock.

        Keep the first stamp. A captured row stays non-dispatchable even while
        its platform processing has not resolved yet."""

        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._select(db, command_id)
            if current is None:
                db.rollback()
                raise RuntimeError("execution journal has no started command to detach")
            try:
                self._require_digest(current, digest)
            except RuntimeError:
                db.rollback()
                raise
            if current.state != "started":
                db.rollback()
                raise RuntimeError("only a started execution can detach")
            if current.detached_at is not None:
                db.commit()
                return current
            db.execute(
                """UPDATE executions SET detached_at = ?, updated_at = ?
                   WHERE command_id = ? AND state = 'started'""",
                (now, now, command_id),
            )
            record = self._select(db, command_id)
            db.commit()
        if record is None or record.detached_at is None:
            raise RuntimeError("execution journal failed to persist detached command")
        return record

    def mark_captured(
        self,
        *,
        command_id: str,
        digest: str,
        outcome: str,
        events: tuple[dict[str, Any], ...],
    ) -> ExecutionRecord:
        """Freeze model output, NOT platform success or a publishable outbox."""
        if outcome not in {"success", "failure", "cancelled"}:
            raise ValueError(f"unsupported capture outcome: {outcome}")
        encoded_capture = _events_json(events)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._select(db, command_id)
            if current is None:
                raise RuntimeError("execution journal has no started command to capture")
            self._require_digest(current, digest)
            if current.capture_outcome is not None:
                stored_capture = db.execute(
                    "SELECT captured_events FROM executions WHERE command_id = ?",
                    (command_id,),
                ).fetchone()[0]
                # Decoded equality conflates JSON true/1 and 1/1.0.
                if current.capture_outcome != outcome or stored_capture != encoded_capture:
                    raise RuntimeError("captured execution journal result is immutable")
                return current
            if current.state != "started":
                raise RuntimeError("execution must be started before capture")
            db.execute(
                """UPDATE executions SET capture_outcome = ?, captured_events = ?,
                       updated_at = ? WHERE command_id = ?""",
                (outcome, encoded_capture, _now(), command_id),
            )
            record = self._select(db, command_id)
        if record is None:
            raise RuntimeError("execution journal failed to persist capture")
        return record

    def poison_capture(self, *, command_id: str, digest: str) -> None:
        """Keep contradictory observer evidence fail-closed across restart."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._select(db, command_id)
            if current is None:
                raise RuntimeError("execution journal has no capture to poison")
            self._require_digest(current, digest)
            if current.state == "started" and current.capture_outcome is not None:
                db.execute(
                    """UPDATE executions SET capture_error = 'conflicting_native_capture',
                           updated_at = ? WHERE command_id = ?""",
                    (_now(), command_id),
                )

    def mark_completed(
        self,
        *,
        command_id: str,
        digest: str,
        outcome: str,
        terminal_events: tuple[dict[str, Any], ...],
        failure_code: str | None = None,
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
                stored_terminal = db.execute(
                    "SELECT terminal_events FROM executions WHERE command_id = ?",
                    (command_id,),
                ).fetchone()[0]
                if (
                    current.outcome != outcome
                    or current.failure_code != failure_code
                    or stored_terminal != encoded_terminal
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
            if current.capture_error is not None and outcome == "success":
                raise RuntimeError("poisoned capture cannot resolve as success")
            db.execute(
                """UPDATE executions
                   SET state = 'completed', outcome = ?, terminal_events = ?,
                       failure_code = ?, updated_at = ?
                   WHERE command_id = ? AND state = 'started'""",
                (outcome, encoded_terminal, failure_code, now, command_id),
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
