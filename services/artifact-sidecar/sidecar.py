"""Sidecar core: state, capture pipeline, outbox and envelope construction.

Durability model (restart-safe at every step):

  1. Bytes are written to the content-addressed store FIRST (fsync +
     link-based no-clobber). An event is never published for bytes that are
     not already durable.
  2. The artifact state row AND the outbox row are committed in ONE sqlite
     transaction (with the processed-source-event ledger row). A crash can
     never strand an artifact row whose event was never enqueued: either both
     exist or neither.
  3. Publication is the outbox's job ONLY. `published` in dispositions and
     logs always means "accepted by the transport". A row that is only
     enqueued is reported as `enqueued`, never as published. The source bus
     message is acked once its capture is durably enqueued, held, classified
     duplicate, or terminal-unqualifiable; the outbox retries publication on
     its own idle timer (not only on the next incoming event), with bounded
     attempts and exponential backoff. Exhausted rows stay visible (nonzero
     `--status` signal, `--retry-outbox` CLI to re-arm them).

Idempotency:

  - Same logical artifact + same bytes at head => duplicate, ack, no event.
  - Same logical artifact + new bytes => revision+1, new event.
  - Processed-source ledger keyed (artifact_id, source_event_id): a replayed
    OLD source event after a newer revision exists is a no-op (the ledger
    hits before the head comparison), so replay can never create a reverted
    revision from stale bytes.
  - Crash between the state commit and the publish => the outbox row still
    holds the envelope; the drain loop publishes it on restart, on the next
    tick, or on the idle retry timer.

The envelope is validated against the canonical JSON Schema (via
core.validate.validate_envelope, the same machinery every hook producer
uses) BEFORE it enters the outbox, so nothing invalid can reach the bus.

Holds: attribution failures are durable rows keyed by a hold_id derived from
(source_event_id, logical_path, sha256) — identity keeps provenance; the same
bytes from two sources are two holds, not one overwritten record.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

_HERE = Path(__file__).resolve().parent
_HOOKS = _HERE.parent / "agent-hooks"
for _p in (str(_HERE), str(_HOOKS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import discovery
from artifact_lib import (
    ARTIFACT_TYPE,
    MAX_CAPTURE_BYTES,
    PathViolation,
    ProjectRegistry,
    StoreError,
    artifact_id_for,
    denied_path,
    get_blob,
    git_repo_identity,
    hold_id_for,
    is_hold_id,
    put_blob,
    safe_resolve_under_root,
)
from discovery import Candidate, Capture

# Imported by entrypoints with services/agent-hooks on sys.path: the repo's
# canonical envelope builder + validator (schema-first, no local reimplementation).
from core.envelope import build_envelope  # type: ignore[import-not-found]
from core.validate import ContractViolation, validate_envelope  # type: ignore[import-not-found]

# Outbox retry contract: bounded attempts, exponential backoff base seconds.
# A row that exhausts attempts stays in the table (visible, nonzero status)
# and is re-armed only by the explicit `--retry-outbox` operator action.
OUTBOX_MAX_ATTEMPTS = 20
OUTBOX_BACKOFF_BASE_S = 2.0


class SidecarError(RuntimeError):
    """Internal sidecar failure (state/store inconsistency)."""


# ---------------------------------------------------------------------------
# State store (sqlite)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    logical_path TEXT NOT NULL,
    revision INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL,
    title TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    published_event_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (artifact_id, revision)
);
CREATE TABLE IF NOT EXISTS processed_sources (
    artifact_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (artifact_id, source_event_id)
);
CREATE TABLE IF NOT EXISTS holds (
    hold_id TEXT PRIMARY KEY,
    record_json TEXT NOT NULL,
    held_at TEXT NOT NULL,
    resolution TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    artifact_id TEXT,
    revision INTEGER,
    envelope_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL,
    last_error TEXT,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox(published_at, next_attempt_at);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SidecarState:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        # Private by default: artifact titles/paths and hold records live here.
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "SidecarState":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- artifacts ---------------------------------------------------------

    def artifact_head(self, artifact_id: str) -> sqlite3.Row | None:
        cur = self._db.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ? ORDER BY revision DESC LIMIT 1",
            (artifact_id,),
        )
        return cur.fetchone()

    def _processed_row(self, artifact_id: str, source_event_id: str) -> sqlite3.Row | None:
        cur = self._db.execute(
            "SELECT * FROM processed_sources WHERE artifact_id = ? AND source_event_id = ?",
            (artifact_id, source_event_id),
        )
        return cur.fetchone()

    def commit_artifact_and_outbox(
        self,
        artifact_row: dict[str, Any],
        source_event_id: str,
        event_id: str,
        envelope: dict[str, Any],
    ) -> bool:
        """Atomically commit artifact head + processed ledger + outbox row.

        Returns False when the event was already enqueued (idempotent
        replay). Any failure rolls the whole transaction back: a crash can
        never leave an artifact row whose event was never enqueued, and can
        never enqueue an event for an artifact row that does not exist.
        """
        artifact_id = artifact_row["artifact_id"]
        revision = int(artifact_row["revision"])
        now = _now()
        try:
            self._db.execute("BEGIN IMMEDIATE")
            prior = self._db.execute(
                "SELECT 1 FROM outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            if prior is not None:
                self._db.execute("ROLLBACK")
                return False
            self._db.execute(
                """
                INSERT INTO artifacts (artifact_id, project_slug, logical_path, revision,
                                       sha256, media_type, title, source_event_id,
                                       published_event_id, created_at)
                VALUES (:artifact_id, :project_slug, :logical_path, :revision,
                        :sha256, :media_type, :title, :source_event_id,
                        NULL, :created_at)
                ON CONFLICT (artifact_id, revision) DO NOTHING
                """,
                artifact_row,
            )
            self._db.execute(
                """
                INSERT INTO processed_sources (artifact_id, source_event_id, sha256, revision, processed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (artifact_id, source_event_id) DO NOTHING
                """,
                (artifact_id, source_event_id, artifact_row["sha256"], revision, now),
            )
            self._db.execute(
                "INSERT INTO outbox (event_id, artifact_id, revision, envelope_json, created_at, next_attempt_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, artifact_id, revision, json.dumps(envelope, ensure_ascii=False), now, 0.0),
            )
            self._db.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            try:
                self._db.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            return False
        except Exception:
            try:
                self._db.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

    def mark_published(self, artifact_id: str, revision: int, event_id: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE artifacts SET published_event_id = ? WHERE artifact_id = ? AND revision = ?",
                (event_id, artifact_id, revision),
            )

    # -- holds ---------------------------------------------------------------

    def add_hold(self, record: dict[str, Any]) -> None:
        record = dict(record)
        record.setdefault("held_at", _now())
        record["held_at"] = record.get("held_at") or _now()
        with self._db:
            self._db.execute(
                "INSERT INTO holds (hold_id, record_json, held_at, resolution) VALUES (?, ?, ?, NULL) "
                "ON CONFLICT (hold_id) DO NOTHING",
                (record["hold_id"], json.dumps(record, ensure_ascii=False), record["held_at"]),
            )

    def holds(self, include_resolved: bool = False) -> list[dict[str, Any]]:
        cur = self._db.execute("SELECT record_json, resolution FROM holds ORDER BY held_at")
        rows = []
        for row in cur.fetchall():
            record = json.loads(row["record_json"])
            record["resolution"] = row["resolution"]
            if row["resolution"] is None or include_resolved:
                rows.append(record)
        return rows

    def hold(self, hold_id: str) -> dict[str, Any] | None:
        cur = self._db.execute(
            "SELECT record_json, resolution FROM holds WHERE hold_id = ?", (hold_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        record = json.loads(row["record_json"])
        record["resolution"] = row["resolution"]
        return record

    def resolve_hold(self, hold_id: str, slug: str) -> bool:
        with self._db:
            cur = self._db.execute("SELECT 1 FROM holds WHERE hold_id = ?", (hold_id,))
            if cur.fetchone() is None:
                return False
            self._db.execute(
                "UPDATE holds SET resolution = ? WHERE hold_id = ?",
                (json.dumps({"slug": slug, "resolved_at": _now()}), hold_id),
            )
            return True

    # -- outbox --------------------------------------------------------------

    def enqueue(self, event_id: str, envelope: dict[str, Any], artifact_id: str | None = None, revision: int | None = None) -> bool:
        """Add to outbox; return False when event_id already enqueued (idempotent)."""
        with self._db:
            cur = self._db.execute("SELECT 1 FROM outbox WHERE event_id = ?", (event_id,))
            if cur.fetchone() is not None:
                return False
            self._db.execute(
                "INSERT INTO outbox (event_id, artifact_id, revision, envelope_json, created_at, next_attempt_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, artifact_id, revision, json.dumps(envelope, ensure_ascii=False), _now(), 0.0),
            )
            return True

    def due(self, max_attempts: int = OUTBOX_MAX_ATTEMPTS) -> list[dict[str, Any]]:
        """Unpublished rows whose backoff has elapsed and attempts remain."""
        cur = self._db.execute(
            "SELECT outbox_id, event_id, artifact_id, revision, envelope_json, attempts, last_error "
            "FROM outbox WHERE published_at IS NULL AND attempts < ? AND "
            "(next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY outbox_id",
            (max_attempts, time.time()),
        )
        return [dict(row) for row in cur.fetchall()]

    def pending(self, max_attempts: int = 10**9) -> list[dict[str, Any]]:
        """Every unpublished row regardless of backoff (status/CLI view)."""
        cur = self._db.execute(
            "SELECT outbox_id, event_id, artifact_id, revision, attempts, last_error "
            "FROM outbox WHERE published_at IS NULL AND attempts < ? ORDER BY outbox_id",
            (max_attempts,),
        )
        return [dict(row) for row in cur.fetchall()]

    def exhausted(self) -> list[dict[str, Any]]:
        cur = self._db.execute(
            "SELECT outbox_id, event_id, artifact_id, revision, attempts, last_error "
            "FROM outbox WHERE published_at IS NULL AND attempts >= ? ORDER BY outbox_id",
            (OUTBOX_MAX_ATTEMPTS,),
        )
        return [dict(row) for row in cur.fetchall()]

    def record_attempt(self, outbox_id: int, error: str | None) -> None:
        """Bump attempts with exponential backoff (2^attempts * base, capped 1h)."""
        row = self._db.execute(
            "SELECT attempts FROM outbox WHERE outbox_id = ?", (outbox_id,)
        ).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        delay = min(OUTBOX_BACKOFF_BASE_S * (2 ** max(0, attempts - 1)), 3600.0)
        with self._db:
            self._db.execute(
                "UPDATE outbox SET attempts = ?, last_error = ?, next_attempt_at = ? WHERE outbox_id = ?",
                (attempts, error, time.time() + delay, outbox_id),
            )

    def mark_outbox_published(self, outbox_id: int) -> None:
        with self._db:
            self._db.execute(
                "UPDATE outbox SET published_at = ?, next_attempt_at = NULL WHERE outbox_id = ?",
                (_now(), outbox_id),
            )

    def _finish_publish(self, row: dict[str, Any]) -> None:
        """Mark an outbox row published and stamp its artifact head."""
        self.mark_outbox_published(int(row["outbox_id"]))
        if row.get("artifact_id"):
            self.mark_published(
                str(row["artifact_id"]), int(row["revision"] or 1), str(row["event_id"])
            )

    def retry_exhausted(self) -> int:
        """Operator action: re-arm every exhausted row (attempts reset, backoff now)."""
        with self._db:
            cur = self._db.execute(
                "UPDATE outbox SET attempts = 0, next_attempt_at = ? "
                "WHERE published_at IS NULL AND attempts >= ?",
                (time.time(), OUTBOX_MAX_ATTEMPTS),
            )
            return cur.rowcount


# ---------------------------------------------------------------------------
# Capture pipeline
# ---------------------------------------------------------------------------


@dataclass
class Disposition:
    """What happened to one candidate from one source event.

    kind `enqueued`   = durably in the outbox, NOT yet published.
    kind `published`  = the transport accepted it.
    """

    kind: str  # published | enqueued | duplicate | held | skipped | unsupported | error
    detail: str = ""
    artifact_id: str | None = None
    revision: int | None = None


class Sidecar:
    def __init__(
        self,
        *,
        state: SidecarState,
        store_root: Path,
        registry: ProjectRegistry,
        service_name: str = "artifact-sidecar",
    ):
        self.state = state
        self.store_root = store_root
        self.registry = registry
        self.service_name = service_name

    # -- project resolution --------------------------------------------------

    def resolve_project(self, candidate: Candidate) -> tuple[dict[str, Any], Path] | None:
        """Resolve the candidate's project through the registry.

        Returns (project_payload, project_repo_root) or None when attribution
        is ambiguous (the caller holds the capture). Attribution precedence:

          1. the event's working_directory resolved against registry
             repo_paths — deepest containment wins — never the sidecar cwd;
          2. the CANONICAL GIT IDENTITY of that directory: a linked worktree
             (e.g. /code/33GOD/bloodbank-artifact-sidecar) is physically
             inside the 33god umbrella but its git common-dir belongs to the
             enrolled bloodbank repo — attribution follows the git identity,
             never the umbrella. When the directory's repository belongs to
             NO enrolled project (a foreign nested repo), attribution is
             ambiguous and the capture is held, never guessed.
        """
        if candidate.project_slug is not None:
            # The source event names its own project (activity report): the
            # registry is the enrollment authority, no cwd is involved.
            entry = self.registry.entry_for_slug(candidate.project_slug)
            if entry is None:
                return None
            repo_path = entry.get("repo_path")
            return (
                {
                    "slug": candidate.project_slug,
                    "name": str(entry.get("name") or candidate.project_slug)[:120],
                },
                Path(str(repo_path)) if repo_path else Path("/"),
            )
        wd = candidate.working_directory
        if not wd:
            return None
        ref = self.registry.resolve_by_repo_path(wd)
        if ref is None:
            return None
        entry = self.registry.entry_for_slug(ref.slug)
        if entry is None:
            return None
        repo_root = Path(str(entry.get("repo_path") or wd))

        identity = git_repo_identity(wd)
        if identity is not None:
            toplevel, common_dir = identity
            picked_git = _git_dir_for(str(repo_root))
            if picked_git is not None and common_dir == picked_git:
                # Same repository (the wd may be a linked worktree of the
                # picked repo): the project stands, and the REPO ROOT for
                # path containment is the wd's own toplevel, because that is
                # where the event's file paths actually live.
                return {"slug": ref.slug, "name": ref.name}, Path(toplevel)
            # The wd's repository is not the containment pick's. If its git
            # identity belongs to ANOTHER enrolled repo, attribute there
            # (the worktree-inside-umbrella case); otherwise ambiguous -> hold.
            owner = self._repo_owner(toplevel, common_dir, exclude_slug=ref.slug)
            if owner is None:
                return None
            owner_entry = self.registry.entry_for_slug(owner)
            if owner_entry is None:
                return None
            return (
                {"slug": owner, "name": str(owner_entry.get("name") or owner)},
                Path(toplevel),
            )
        return {"slug": ref.slug, "name": ref.name}, repo_root

    def _repo_owner(
        self, toplevel: str, common_dir: str, *, exclude_slug: str
    ) -> str | None:
        """The enrolled slug whose repository this git identity belongs to."""
        for entry in self.registry._projects.values():
            if not isinstance(entry, dict):
                continue
            slug = entry.get("slug")
            if not isinstance(slug, str) or slug == exclude_slug:
                continue
            rp = entry.get("repo_path")
            if not isinstance(rp, str) or not rp:
                continue
            try:
                rp_real = str(Path(rp).resolve(strict=False))
            except OSError:
                continue
            if toplevel == rp_real or common_dir == _git_dir_for(rp):
                return slug
        return None

    # -- the pipeline ----------------------------------------------------------

    def process(self, envelope: dict[str, Any]) -> list[Disposition]:
        """Process one agent event; returns dispositions for each candidate."""
        ce_type = envelope.get("type")
        dispositions: list[Disposition] = []
        candidates: list[Candidate] = []
        if ce_type == "bloodbank.agent.tool.completed":
            single = discovery.from_tool_completed(envelope)
            if single is not None:
                candidates.append(single)
            elif discovery.write_like_but_unsupported(envelope):
                dispositions.append(
                    Disposition(
                        kind="unsupported",
                        detail="write-family tool call without full inline content; no artifact inferred",
                    )
                )
            elif discovery.write_with_content_but_refused(envelope):
                dispositions.append(
                    Disposition(
                        kind="skipped",
                        detail=(
                            "write carried path+content but was refused: unsupported "
                            "extension, binary-from-inline, denied content location, "
                            "or no explicit user-facing signal (delivery marker / "
                            "producer output tree)"
                        ),
                    )
                )
        elif ce_type == "bloodbank.conversation.turn.completed":
            try:
                candidates.extend(discovery.from_turn_completed(envelope))
            except Exception as exc:  # noqa: BLE001 - adapter blowup contained
                dispositions.append(
                    Disposition(kind="error", detail=f"adapter failed: {type(exc).__name__}: {exc}")
                )
        elif ce_type == discovery.ACTIVITY_RECORDED_TYPE:
            try:
                candidates.extend(discovery.from_activity_recorded(envelope))
            except Exception as exc:  # noqa: BLE001 - adapter blowup contained
                dispositions.append(
                    Disposition(kind="error", detail=f"adapter failed: {type(exc).__name__}: {exc}")
                )

        for candidate in candidates:
            try:
                dispositions.append(self._process_candidate(envelope, candidate))
            except Exception as exc:  # noqa: BLE001 - one candidate must not kill its siblings
                dispositions.append(
                    Disposition(kind="error", detail=f"candidate processing failed: {type(exc).__name__}: {exc}")
                )
        return dispositions

    def _hold(
        self,
        envelope: dict[str, Any],
        candidate: Candidate,
        reason: str,
        sha: str,
        ref: str,
    ) -> Disposition:
        record = discovery.hold_record_from(
            envelope, candidate, reason=reason, sha256=sha, ref=ref
        )
        self.state.add_hold(record)
        return Disposition(kind="held", detail=reason)

    def _process_candidate(
        self, envelope: dict[str, Any], candidate: Candidate
    ) -> Disposition:
        if len(candidate.capture.data) > MAX_CAPTURE_BYTES:
            return Disposition(kind="skipped", detail="capture exceeds size bound")
        if (
            candidate.project_slug is not None
            and self.registry.entry_for_slug(candidate.project_slug) is None
        ):
            # An activity report names its own project; a slug the pjangler
            # registry does not enroll is not guessed or held (a hold would
            # also drop the html twin). Observable, nothing persisted.
            return Disposition(
                kind="skipped",
                detail=(
                    f"project slug {candidate.project_slug!r} named by the source event "
                    "is not enrolled in the pjangler registry; not persisted"
                ),
            )
        render = candidate.capture.render
        if render is not None and (
            len(render.data) > MAX_CAPTURE_BYTES or render.data == candidate.capture.data
        ):
            render = None  # oversize, or not a distinct byte sequence: no twin

        # 0. Path hygiene BEFORE persistence: the denylist applies to inline
        #    logical paths too (.env, config/, secrets/, scratch/ … qualify
        #    as denied CONTENT locations even when the bytes arrive inline).
        #    The authoritative check is POST-resolution below, on the
        #    project-relative logical path; this pre-check only short-circuits
        #    the store write for obviously denied inline locations (inline/
        #    paths and relative paths judged as given).
        lp = candidate.capture.logical_path
        pre_rel = artifact_lib_logical_rel(lp, candidate.working_directory) if candidate.working_directory else lp
        if lp.startswith(("inline/", "delivered/")) or not candidate.working_directory:
            fragment = denied_path(pre_rel)
            if fragment is not None:
                return Disposition(
                    kind="skipped",
                    detail=f"logical path matches denied fragment {fragment!r}; not persisted",
                )

        # 1. Bytes durable FIRST. The content is inline in the event; no
        #    local file read happens for capture.
        sha, _blob = put_blob(self.store_root, candidate.capture.data)
        ref = f"sha256:{sha}"
        if render is not None:
            put_blob(self.store_root, render.data)

        # 2. Resolve attribution from the registry; never guess.
        resolved = self.resolve_project(candidate)
        if resolved is None:
            return self._hold(
                envelope, candidate,
                "working_directory absent, not under any indexed-ok registry repo_path, "
                "or attribution ambiguous (nested/worktree identity)",
                sha, ref,
            )
        project, repo_root = resolved

        # 3. Logical path: project-relative for file writes, inline/ and
        #    delivered/ for extracted or explicitly delivered content. A
        #    file_path escaping the repo root is held, not published
        #    (path-traversal defence; bytes are inline so this is an
        #    attribution check, not a read).
        logical_path = candidate.capture.logical_path
        if not logical_path.startswith(("inline/", "delivered/")):
            try:
                safe_resolve_under_root(Path(logical_path), repo_root, label="logical")
                logical_path = artifact_lib_logical_path(logical_path, str(repo_root))
            except (PathViolation, ValueError):
                return self._hold(
                    envelope, candidate, "file_path outside the resolved project repo root",
                    sha, ref,
                )
        else:
            fragment = denied_path(logical_path)
            if fragment is not None:
                return Disposition(
                    kind="skipped",
                    detail=f"logical path matches denied fragment {fragment!r}; not persisted",
                )

        artifact_id = artifact_id_for(project["slug"], logical_path)
        source_event_id = str(envelope.get("id"))

        # 3b. Authoritative denylist on the PROJECT-RELATIVE logical path,
        #     before any state commit: repo-level .env/config/secrets/scratch
        #     content is never persisted wherever the checkout lives.
        fragment = denied_path(logical_path)
        if fragment is not None:
            return Disposition(
                kind="skipped",
                detail=f"logical path matches denied fragment {fragment!r}; not persisted",
            )

        # 3a. Processed-source ledger: an OLD source event replayed after a
        #     newer revision exists is a no-op — the head sha comparison
        #     alone would mint a bogus reverted revision.
        ledger = self.state._processed_row(artifact_id, source_event_id)
        if ledger is not None:
            return Disposition(
                kind="duplicate",
                detail=f"source event already processed at revision {ledger['revision']}",
                artifact_id=artifact_id,
                revision=int(ledger["revision"]),
            )

        prior = self.state.artifact_head(artifact_id)
        if prior is not None and prior["sha256"] == sha:
            return Disposition(
                kind="duplicate",
                detail=f"bytes unchanged at revision {prior['revision']}",
                artifact_id=artifact_id,
                revision=prior["revision"],
            )
        revision = (prior["revision"] + 1) if prior is not None else 1

        # 4. Build + validate the envelope BEFORE touching state or outbox.
        event = self._build_event(
            envelope, candidate, project, artifact_id, revision, sha, ref
        )
        try:
            validate_envelope(event)
        except (ContractViolation, Exception) as exc:  # noqa: BLE001
            return Disposition(kind="error", detail=f"envelope failed validation: {exc}")

        # 5. Atomic state+outbox commit; the drain loop publishes.
        artifact_row = {
            "artifact_id": artifact_id,
            "project_slug": project["slug"],
            "logical_path": logical_path,
            "revision": revision,
            "sha256": sha,
            "media_type": candidate.capture.media_type,
            "title": candidate.capture.title,
            "source_event_id": source_event_id,
            "published_event_id": None,
            "created_at": event["data"]["origin"]["created_at"],
        }
        inserted = self.state.commit_artifact_and_outbox(
            artifact_row, source_event_id, event["id"], event
        )
        if not inserted:
            return Disposition(kind="duplicate", detail="event already enqueued",
                               artifact_id=artifact_id, revision=revision)
        return Disposition(
            kind="enqueued",
            detail="durably enqueued for publication",
            artifact_id=artifact_id,
            revision=revision,
        )

    def _build_event(
        self,
        source: dict[str, Any],
        candidate: Candidate,
        project: dict[str, Any],
        artifact_id: str,
        revision: int,
        sha: str,
        ref: str,
    ) -> dict[str, Any]:
        capture = candidate.capture
        source_data = source.get("data") or {}
        actor = source.get("actor") or {}
        artifact_block: dict[str, Any] = {
            "artifact_id": artifact_id,
            "revision": revision,
            "title": capture.title,
            "media_type": capture.media_type,
            "sha256": sha,
            "bytes": len(capture.data),
            "content": {"storage": "content-addressed", "ref": ref},
        }
        twin = capture.render
        if twin is not None and twin.data != capture.data and len(twin.data) <= MAX_CAPTURE_BYTES:
            twin_sha = hashlib.sha256(twin.data).hexdigest()
            artifact_block["render"] = {
                "media_type": twin.media_type,
                "sha256": twin_sha,
                "bytes": len(twin.data),
                "content": {"storage": "content-addressed", "ref": f"sha256:{twin_sha}"},
            }
        return build_envelope(
            ce_type=ARTIFACT_TYPE,
            kind="event",
            source=f"urn:33god:service:{self.service_name}",
            producer=self.service_name,
            service=self.service_name,
            actor={
                "type": "service",
                "agent_id": f"bloodbank.service.{self.service_name}",
                "cli": None,
                "provider": None,
                "model": None,
            },
            correlation_id=str(source.get("correlationid") or uuid.uuid4()),
            causation_id=str(source.get("id")),
            data={
                "schema_version": 1,
                "project": project,
                "artifact": artifact_block,
                "origin": {
                    "detected_by": self.service_name,
                    "basis": candidate.basis,
                    "qualification_reason": candidate.qualification_reason,
                    "source_event": {
                        "type": str(source.get("type")),
                        "id": str(source.get("id")),
                        "time": str(source.get("time")),
                    },
                    "session": {
                        "producer": str(source.get("producer") or "unknown"),
                        "cli": actor.get("cli") if isinstance(actor.get("cli"), str) else None,
                        "thread_id": _str_or_none(source_data.get("thread_id")),
                        "invocation_id": _str_or_none(source_data.get("invocation_id")),
                    },
                    "created_at": str(source.get("time")),
                },
            },
            ordering_key=f"project:{project['slug']}",
            validate=False,
        )

    # -- outbox drain -----------------------------------------------------------

    def drain_outbox(self, publish: Callable[[dict[str, Any]], Any]) -> list[Disposition]:
        """Publish every DUE outbox envelope through the SYNC `publish` callable.

        A row is due when its backoff has elapsed and attempts remain. The
        publish callable's success means THE TRANSPORT ACCEPTED the message;
        only then is the row marked published. Any exception records the
        attempt (bounded, with backoff) and leaves the row pending — errors
        are never swallowed into a published state. Async transports use
        drain_outbox_async instead.
        """
        results: list[Disposition] = []
        for row in self.state.due():
            envelope = json.loads(row["envelope_json"])
            try:
                publish(envelope)
            except Exception as exc:  # noqa: BLE001 - any transport error retries later
                self.state.record_attempt(row["outbox_id"], f"{type(exc).__name__}: {exc}")
                results.append(
                    Disposition(kind="error", detail=f"outbox publish failed: {exc}")
                )
                continue
            self.state._finish_publish(row)
            results.append(Disposition(kind="published", detail="published", artifact_id=row["artifact_id"]))
        return results

    async def drain_outbox_async(
        self, publish: Callable[[dict[str, Any]], Awaitable[Any]]
    ) -> list[Disposition]:
        """Awaited twin of drain_outbox for real async transports."""
        results: list[Disposition] = []
        for row in self.state.due():
            envelope = json.loads(row["envelope_json"])
            try:
                await publish(envelope)
            except Exception as exc:  # noqa: BLE001
                self.state.record_attempt(row["outbox_id"], f"{type(exc).__name__}: {exc}")
                results.append(
                    Disposition(kind="error", detail=f"outbox publish failed: {exc}")
                )
                continue
            self.state._finish_publish(row)
            results.append(Disposition(kind="published", detail="published", artifact_id=row["artifact_id"]))
        return results

    # -- held-capture resolution ---------------------------------------------

    def publish_resolution(self, hold_id: str) -> Disposition:
        """Publish a previously held capture after an operator resolved it."""
        record = self.state.hold(hold_id)
        if record is None:
            return Disposition(kind="error", detail=f"no hold with id {hold_id}")
        resolution = record.get("resolution")
        if isinstance(resolution, str):
            try:
                resolution = json.loads(resolution)
            except json.JSONDecodeError:
                resolution = None
        if not isinstance(resolution, dict) or not resolution:
            return Disposition(kind="error", detail="hold has no resolution; resolve it first")
        slug = str(resolution.get("slug"))
        entry = self.registry.entry_for_slug(slug)
        if entry is None:
            return Disposition(kind="error", detail=f"slug {slug!r} not in registry")
        project = {"slug": slug, "name": str(entry.get("name") or slug)}
        try:
            data = get_blob(self.store_root, str(record["ref"]))
        except StoreError as exc:
            return Disposition(kind="error", detail=str(exc))
        logical_path = str(record.get("logical_path") or "") or f"held/{str(record['hold_id'])[:16]}"
        artifact_id = artifact_id_for(project["slug"], logical_path)
        source_event_id = str(record["source_event"]["id"])
        ledger = self.state._processed_row(artifact_id, source_event_id)
        prior = self.state.artifact_head(artifact_id)
        if (ledger is not None) or (prior is not None and prior["sha256"] == record["sha256"]):
            return Disposition(kind="duplicate", detail="already published", artifact_id=artifact_id)
        revision = (prior["revision"] + 1) if prior is not None else 1
        source = {
            "id": source_event_id,
            "type": record["source_event"]["type"],
            "time": record["source_event"]["time"],
            "correlationid": None,
            "producer": record.get("producer"),
        }
        candidate = Candidate(
            basis="operator_resolution",
            qualification_reason=f"operator resolved held capture ({record['reason']}) to {slug}",
            capture=Capture(
                title=str(record["title"]),
                logical_path=logical_path,
                media_type=str(record["media_type"]),
                data=data,
            ),
            working_directory=record.get("working_directory"),
        )
        event = self._build_event(
            source, candidate, project, artifact_id, revision,
            str(record["sha256"]), str(record["ref"]),
        )
        validate_envelope(event)
        artifact_row = {
            "artifact_id": artifact_id,
            "project_slug": project["slug"],
            "logical_path": logical_path,
            "revision": revision,
            "sha256": str(record["sha256"]),
            "media_type": str(record["media_type"]),
            "title": str(record["title"]),
            "source_event_id": source_event_id,
            "published_event_id": None,
            "created_at": event["data"]["origin"]["created_at"],
        }
        inserted = self.state.commit_artifact_and_outbox(
            artifact_row, source_event_id, event["id"], event
        )
        if not inserted:
            return Disposition(kind="duplicate", detail="event already enqueued",
                               artifact_id=artifact_id, revision=revision)
        return Disposition(kind="enqueued", detail="durably enqueued for publication",
                           artifact_id=artifact_id, revision=revision)


def artifact_lib_logical_path(absolute_path: str, project_root: str) -> str:
    from artifact_lib import logical_path_for

    return logical_path_for(absolute_path, project_root)


def artifact_lib_logical_rel(path: str, working_directory: str) -> str:
    """Path relative to working_directory when derivable, else as given."""
    from pathlib import PurePosixPath

    try:
        return PurePosixPath(path.replace("\\", "/")).relative_to(
            PurePosixPath(working_directory.replace("\\", "/"))
        ).as_posix()
    except ValueError:
        return path


def _git_dir_for(repo_path: str) -> str | None:
    """Real path of a repo's .git dir ('.git' file indirection included), or None."""
    identity = git_repo_identity(repo_path)
    if identity is None:
        return None
    _top, common_dir = identity
    return common_dir


def _run_maybe_future(fut: Any, loop: Any) -> Any:
    """Await a future inside a running loop via a fresh task + wait."""
    import asyncio

    task = asyncio.ensure_future(fut, loop=loop)
    done = asyncio.wait({task})
    return task.result()


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
