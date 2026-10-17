"""artifact-vault-projector: copies published artifacts into the project vault.

A SEPARATE bus consumer from the sidecar (no direct calls, no shared
process): it subscribes to bloodbank.evt.project.artifact.created on a
dedicated JetStream durable, validates the FULL canonical schema +
invariants before any write, reads the bytes from the sidecar's
content-addressed store (read-only, digest-verified, size-bounded), and
writes them under the operator's configured canonical vault project path:

    <vault-root>/<mapped project dir>/<YYYY-MM-DD>/<title>

Safety contract (every point is tested):

  - Malformed envelopes are rejected by the canonical full schema +
    invariants (core.validate.validate_envelope) BEFORE any store read or
    file write; they are dead-lettered (durable row + explicit CLI), not
    acked-and-lost and not retried forever.
  - Unmapped slugs are durable holds with an explicit resolve CLI. They are
    never acked-and-lost and never guessed into a shadow directory.
  - VaultWriter traverses project/date components through directory fds with
    O_NOFOLLOW — a symlink planted anywhere below the root is refused, not
    followed; nothing can redirect a write outside the root at any race.
  - Files land via O_EXCL tmp + fsync + atomic NO-CLOBBER hard link: a
    competing writer (human or process) can never be overwritten. A
    conflicting existing file gets a deterministic content-addressed suffix
    `-<sha8>` (same bytes always the same name, so replay after a crash
    finds the identical file instead of duplicating); the ACTUAL chosen path
    is what gets recorded in the projection state and returned.
  - Crash between file write and DB record: the deterministic suffix means
    the replay re-derives the same target, sees identical bytes, and records
    it — no duplicate file.
  - Source AND render lengths/digests are validated before either write;
    a missing render blob is a retryable error (never marked projected);
    the render file extension comes from its own media type, not the
    source's.
  - The date folder comes from origin.created_at (the SOURCE event's time,
    not publication time) converted to the configured timezone.
  - No loop: the projector publishes nothing, so it cannot re-enter the
    sidecar.

Usage:
  python3 main.py               # run the consumer loop
  python3 main.py --status      # projection state snapshot (nonzero on unresolved)
  python3 main.py --plan        # show mapping + rules without consuming
  python3 main.py --holds       # list unmapped/malformed dead-letter holds
  python3 main.py --hold-resolve-map <slug> <vault-dir>
      # record an explicit mapping for a held unmapped slug (writes the
      # mapping file), then the hold is projectable with --hold-project

Environment:
  ARTIFACT_VAULT_NATS_URL     default nats://127.0.0.1:4222
  ARTIFACT_VAULT_STATE_DB     default ~/.local/state/artifact-vault-projector/state.sqlite3
  ARTIFACT_VAULT_MAPPING      required for live runs: explicit JSON mapping file
  ARTIFACT_VAULT_STORE        the sidecar's content store (read-only use)
  ARTIFACT_VAULT_TZ           default America/New_York
  ARTIFACT_VAULT_DURABLE      default bloodbank-artifact-vault-projector
  ARTIFACT_VAULT_BACKFILL     "1" => new durable created with DeliverPolicy.ALL
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sqlite3
import stat as stat_module
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
_SIDECAR_DIR = _HERE.parent / "artifact-sidecar"
for _p in (str(_HERE), str(_SIDECAR_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import nats
import nats.js.errors
from nats.aio.msg import Msg
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

from artifact_lib import (
    ARTIFACT_TYPE,
    MAX_CAPTURE_BYTES,
    StoreError,
    get_blob,
    sha256_bytes,
)
from sidecar import SidecarState  # state-db conventions shared for perms

sys.path.insert(0, str(_HERE.parents[1] / "agent-hooks"))
from core.validate import ContractViolation, validate_envelope  # type: ignore[import-not-found] # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [artifact-vault-projector] %(levelname)s %(message)s",
)
log = logging.getLogger("artifact-vault-projector")

EVENTS_STREAM = "BLOODBANK_EVENTS"
PROJECTOR_SUBJECT = "bloodbank.evt.project.artifact.created"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projections (
    event_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    project_slug TEXT NOT NULL,
    vault_path TEXT NOT NULL,
    projected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holds (
    hold_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    event_id TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    held_at TEXT NOT NULL,
    resolution TEXT
);
CREATE INDEX IF NOT EXISTS idx_holds_unresolved ON holds(resolution) WHERE resolution IS NULL;
"""

_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+,@^-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class MappingError(ValueError):
    """The vault mapping is missing, malformed, or unsafe."""


def load_mapping(path: str | None) -> dict[str, str]:
    """Load and validate the explicit slug -> vault project dir mapping.

    Rules: a JSON object; keys are registry slugs; values are existing
    directories directly under the vault root. Values must not be absolute,
    must not escape the root (.. or leading slash), and must not collide
    after case-folding (a plain Syncthing tree on Linux is case-sensitive
    today, but the operator's canonical names come from DeLoDocs/AGENTS.md
    and we refuse ambiguity at config time).
    """
    if not path:
        raise MappingError(
            "ARTIFACT_VAULT_MAPPING is required: projection targets are "
            "explicit operator config, never guessed"
        )
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MappingError(f"cannot read mapping {path}: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise MappingError("mapping must be a non-empty JSON object of slug -> directory")
    raw = {k: v for k, v in raw.items() if not k.startswith("$")}
    seen_folded: dict[str, str] = {}
    mapping: dict[str, str] = {}
    for slug, target in raw.items():
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise MappingError(f"mapping key {slug!r} is not a canonical slug")
        if not isinstance(target, str) or not target:
            raise MappingError(f"mapping[{slug!r}] must be a non-empty string")
        parts = [p for p in Path(target).parts if p not in ("/", "\\")]
        if target.startswith("/") or target.startswith("\\") or ".." in Path(target).parts:
            raise MappingError(
                f"mapping[{slug!r}] = {target!r} must be a directory name relative "
                "to the vault root, not an absolute path or traversal"
            )
        if any(p.startswith(".") and p not in (".", "..") for p in parts):
            raise MappingError(f"mapping[{slug!r}] = {target!r} has hidden components")
        folded = target.casefold()
        if folded in seen_folded:
            raise MappingError(
                f"mapping[{slug!r}] = {target!r} case-collides with "
                f"{seen_folded[folded]!r}"
            )
        seen_folded[folded] = target
        mapping[slug] = target
    return mapping


def event_date_folder(event_time: str, tz_name: str) -> str:
    """YYYY-MM-DD of the event time in the configured zone (vault local day)."""
    value = event_time.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        # Envelope times are UTC by contract; a naive value is treated as UTC.
        from datetime import timezone

        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(ZoneInfo(tz_name))
    return local.strftime("%Y-%m-%d")


def safe_filename(title: str) -> str:
    """A filesystem-safe name derived from the artifact title."""
    name = title.strip().replace("/", "-").replace("\\", "-").replace("\x00", "")
    name = re.sub(r"\s+", " ", name)
    if not name or name in (".", ".."):
        name = "artifact"
    if not _SAFE_NAME_RE.match(name):
        name = "artifact"
    return name[:120]


def _open_dir_nofollow(name: str, dir_fd: int | None = None) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def _fsync_dir_by_fd(fd: int) -> None:
    os.fsync(fd)


class VaultWriter:
    """Writes artifact bytes under the vault root; never overwrites anything.

    Race-safe by construction:
      - every path component below the root is opened as a directory through
        the parent's fd with O_NOFOLLOW (a symlinked project/date dir is
        refused, never followed);
      - destination files are created via O_EXCL tmp + fsync + atomic hard
        link (link(2) fails with EEXIST when a competitor won the name, so
        an existing file — human-written or otherwise — can NEVER be
        replaced, at any interleaving);
      - directories are fsynced after creation and after each file link so a
        crash cannot lose a completed write.
    """

    def __init__(self, vault_root: Path, mapping: dict[str, str]):
        if vault_root.is_symlink():
            raise MappingError(f"vault root {vault_root} is a symlink; refusing")
        vault_root.mkdir(parents=True, exist_ok=True)
        self.root = vault_root.resolve(strict=False)
        self.mapping = mapping

    def project_dir_for(self, slug: str) -> Path:
        target = self.mapping.get(slug)
        if target is None:
            raise MappingError(
                f"project slug {slug!r} has no explicit vault mapping; holding"
            )
        return self.root / target

    def destination(self, *, slug: str, date_folder: str, title: str, render: bool = False, render_media_type: str | None = None) -> Path:
        if not _DATE_RE.match(date_folder):
            raise MappingError(f"bad date folder {date_folder!r}")
        directory = self.project_dir_for(slug) / date_folder
        name = safe_filename(title)
        if render:
            stem = Path(name).stem
            from artifact_lib import MEDIA_TYPE_EXTENSIONS

            ext = MEDIA_TYPE_EXTENSIONS.get(str(render_media_type or ""), None)
            if ext is None:
                ext = Path(name).suffix or ".render"
            name = f"{stem}.render{ext}"
        return directory / name

    def _ensure_dir_walk(self, components: list[str]) -> int:
        """Open the vault root then walk components (mkdir+O_NOFOLLOW each)."""
        try:
            fd = _open_dir_nofollow(str(self.root))
        except OSError as exc:
            raise MappingError(f"vault root {self.root} refused (symlink?): {exc}") from exc
        for comp in components:
            try:
                child = _open_dir_nofollow(comp, fd)
            except FileNotFoundError:
                try:
                    os.mkdir(comp, 0o755, dir_fd=fd)
                    _fsync_dir_by_fd(fd)
                    child = _open_dir_nofollow(comp, fd)
                except OSError as exc:
                    os.close(fd)
                    raise MappingError(f"vault path component {comp!r} unusable: {exc}") from exc
            except OSError as exc:  # ELOOP (symlink), ENOTDIR, …
                os.close(fd)
                raise MappingError(f"vault path component {comp!r} refused (symlink?): {exc}") from exc
            os.close(fd)
            fd = child
        return fd

    def _link_no_clobber(self, dir_fd: int, tmp_name: str, final_name: str) -> bool:
        """Atomically link tmp -> final; False when final already exists."""
        try:
            os.link(tmp_name, final_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            _fsync_dir_by_fd(dir_fd)
            return True
        except FileExistsError:
            return False

    def _read_file_via_fd(self, dir_fd: int, name: str) -> bytes | None:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            # ELOOP (symlink at the destination) etc.: refuse, never follow.
            raise MappingError(f"vault entry {name!r} refused (symlink?): {exc}") from exc
        try:
            st = os.fstat(fd)
            if not stat_module.S_ISREG(st.st_mode):
                raise MappingError(f"vault entry {name!r} is not a regular file")
            if st.st_size > MAX_CAPTURE_BYTES:
                raise MappingError(f"vault entry {name!r} exceeds size bound")
            chunks = []
            total = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_CAPTURE_BYTES:
                    raise MappingError(f"vault entry {name!r} exceeds size bound")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def write(self, *, path: Path, data: bytes, sha: str) -> tuple[str, Path]:
        """Write data at path; never overwrite an existing file.

        Returns (outcome, actual_path): outcome is 'written' | 'identical' |
        'kept-new'. A conflicting existing file (different bytes) keeps its
        name; the new bytes go to a DETERMINISTIC content-addressed sibling
        `<stem>-<sha8><suffix>` — same content always lands at the same
        suffix, so a crash between file and DB record replays to the same
        path instead of duplicating. Refuses symlinks anywhere on the path.
        """
        rel = path.relative_to(self.root)
        components = rel.parts[:-1]
        final_name = rel.parts[-1]
        stem = Path(final_name).stem
        suffix = Path(final_name).suffix
        dir_fd = self._ensure_dir_walk(list(components))
        try:
            try:
                existing = self._read_file_via_fd(dir_fd, final_name)
            except MappingError:
                raise
            except OSError as exc:
                raise MappingError(f"vault destination {final_name!r} refused: {exc}") from exc
            if existing is not None and sha256_bytes(existing) == sha:
                return "identical", path
            if existing is not None:
                # Deterministic content-addressed sibling; verify an existing
                # sibling of the same name has OUR bytes (it always should).
                cand = f"{stem}-{sha[:8]}{suffix}"
                cand_existing = self._read_file_via_fd(dir_fd, cand)
                if cand_existing is not None:
                    if sha256_bytes(cand_existing) == sha:
                        return "kept-new", path.with_name(cand)
                    raise MappingError(
                        f"vault suffix collision at {cand!r} with different bytes"
                    )
                tmp = f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
                tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dir_fd)
                try:
                    os.write(tfd, data)
                    os.fsync(tfd)
                finally:
                    os.close(tfd)
                try:
                    if self._link_no_clobber(dir_fd, tmp, cand):
                        return "kept-new", path.with_name(cand)
                    if self._read_file_via_fd(dir_fd, cand) == data:
                        return "kept-new", path.with_name(cand)
                    raise MappingError(f"vault race on suffix {cand!r}")
                finally:
                    try:
                        os.unlink(tmp, dir_fd=dir_fd)
                    except OSError:
                        pass
            # No existing file: O_EXCL tmp + link no-clobber.
            tmp = f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dir_fd)
            try:
                os.write(tfd, data)
                os.fsync(tfd)
            finally:
                os.close(tfd)
            try:
                if self._link_no_clobber(dir_fd, tmp, final_name):
                    return "written", path
                # Lost the race: identical bytes means done; different bytes
                # means a competitor owns the name -> deterministic suffix.
                raced = self._read_file_via_fd(dir_fd, final_name)
                if raced is not None and sha256_bytes(raced) == sha:
                    return "identical", path
                cand = f"{stem}-{sha[:8]}{suffix}"
                if self._link_no_clobber(dir_fd, tmp, cand):
                    return "kept-new", path.with_name(cand)
                if self._read_file_via_fd(dir_fd, cand) == data:
                    return "kept-new", path.with_name(cand)
                raise MappingError(f"vault race exhausted at {path}")
            finally:
                try:
                    os.unlink(tmp, dir_fd=dir_fd)
                except OSError:
                    pass
        finally:
            os.close(dir_fd)


class ProjectorState:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def seen(self, event_id: str) -> bool:
        cur = self._db.execute("SELECT 1 FROM projections WHERE event_id = ?", (event_id,))
        return cur.fetchone() is not None

    def record(self, row: dict[str, Any]) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO projections (event_id, artifact_id, revision, project_slug, vault_path, projected_at) "
                "VALUES (:event_id, :artifact_id, :revision, :project_slug, :vault_path, :projected_at)",
                row,
            )

    def add_hold(self, *, kind: str, reason: str, event_id: str, envelope: dict[str, Any]) -> str:
        hold_id = uuid.uuid5(uuid.NAMESPACE_URL, f"vault-hold:{kind}:{event_id}").hex
        with self._db:
            self._db.execute(
                "INSERT INTO holds (hold_id, kind, reason, event_id, envelope_json, held_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (hold_id) DO NOTHING",
                (hold_id, kind, reason, event_id, json.dumps(envelope, ensure_ascii=False), _now()),
            )
        return hold_id

    def holds(self, include_resolved: bool = False) -> list[dict[str, Any]]:
        cur = self._db.execute("SELECT * FROM holds ORDER BY held_at")
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            if d.get("resolution") is None or include_resolved:
                d.pop("envelope_json", None)
                rows.append(d)
        return rows

    def hold_envelope(self, hold_id: str) -> dict[str, Any] | None:
        cur = self._db.execute("SELECT envelope_json FROM holds WHERE hold_id = ?", (hold_id,))
        row = cur.fetchone()
        return json.loads(row["envelope_json"]) if row else None

    def resolve_hold(self, hold_id: str, resolution: dict[str, Any]) -> bool:
        with self._db:
            cur = self._db.execute("SELECT 1 FROM holds WHERE hold_id = ?", (hold_id,))
            if cur.fetchone() is None:
                return False
            self._db.execute(
                "UPDATE holds SET resolution = ? WHERE hold_id = ?",
                (json.dumps(resolution), hold_id),
            )
            return True

    def counts(self) -> dict[str, Any]:
        cur = self._db.execute(
            "SELECT project_slug, COUNT(*) AS n FROM projections GROUP BY project_slug"
        )
        return {row["project_slug"]: row["n"] for row in cur.fetchall()}

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "ProjectorState":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class Projector:
    def __init__(self, *, state: ProjectorState, writer: VaultWriter, store_root: Path, tz: str):
        self.state = state
        self.writer = writer
        self.store_root = store_root
        self.tz = tz

    def handle(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Project one artifact.created event. Returns a status dict.

        Outcomes: projected | duplicate | ignored | held (durable, needs
        operator mapping) | deadletter (malformed envelope, durable) |
        error (transient: missing blob, store failure — nak/retry).
        """
        if envelope.get("type") != ARTIFACT_TYPE:
            return {"kind": "ignored", "detail": f"foreign type {envelope.get('type')!r}"}
        event_id = str(envelope.get("id"))
        if self.state.seen(event_id):
            return {"kind": "duplicate", "detail": "event already projected"}

        # Canonical FULL schema + invariants BEFORE any store read or write.
        try:
            validate_envelope(envelope)
        except (ContractViolation, Exception) as exc:  # noqa: BLE001
            hold_id = self.state.add_hold(
                kind="malformed",
                reason=f"schema/invariant violation: {exc}",
                event_id=event_id,
                envelope=envelope,
            )
            return {
                "kind": "deadletter",
                "detail": f"malformed envelope dead-lettered ({hold_id[:8]}…): {exc}",
            }

        data = envelope.get("data") or {}
        project = data.get("project") or {}
        artifact = data.get("artifact") or {}
        slug = project.get("slug")
        if not isinstance(slug, str):
            return {"kind": "error", "detail": "event has no project slug"}
        if slug not in self.writer.mapping:
            hold_id = self.state.add_hold(
                kind="unmapped",
                reason=f"slug {slug!r} has no explicit vault mapping",
                event_id=event_id,
                envelope=envelope,
            )
            return {
                "kind": "held",
                "detail": (
                    f"slug {slug!r} has no explicit vault mapping; held ({hold_id[:8]}…), "
                    "never a guessed directory"
                ),
            }

        # Source blob: read + verify digest and declared length BEFORE writes.
        try:
            payload = get_blob(self.store_root, str(artifact.get("content", {}).get("ref", "")))
        except StoreError as exc:
            return {"kind": "error", "detail": f"source blob unreadable (retry): {exc}"}
        if sha256_bytes(payload) != artifact.get("sha256"):
            return {"kind": "error", "detail": "artifact sha mismatch against blob"}
        if artifact.get("bytes") is not None and int(str(artifact.get("bytes"))) != len(payload):
            return {
                "kind": "error",
                "detail": f"declared bytes {artifact.get('bytes')} != actual {len(payload)}",
            }

        # Render blob (when declared): read + verify BEFORE any write, so a
        # missing render cannot leave a half pair; a missing render is a
        # retryable error, never a 'projected'.
        render_payload: bytes | None = None
        render_media: str | None = None
        render_sha: str | None = None
        render = artifact.get("render")
        if isinstance(render, dict) and isinstance(render.get("content"), dict):
            try:
                render_payload = get_blob(self.store_root, str(render["content"].get("ref", "")))
            except StoreError as exc:
                return {"kind": "error", "detail": f"render blob unreadable (retry): {exc}"}
            if sha256_bytes(render_payload) != render.get("sha256"):
                return {"kind": "error", "detail": "render sha mismatch against blob"}
            if render.get("bytes") is not None and int(str(render.get("bytes"))) != len(render_payload):
                return {
                    "kind": "error",
                    "detail": f"render declared bytes {render.get('bytes')} != actual {len(render_payload)}",
                }
            render_media = str(render.get("media_type") or "")
            render_sha = str(render.get("sha256") or "")

        date_value = str(
            (data.get("origin") or {}).get("created_at") or envelope.get("time") or ""
        )
        date_folder = event_date_folder(date_value, self.tz)
        title = str(artifact.get("title") or "artifact")
        try:
            base = self.writer.destination(slug=slug, date_folder=date_folder, title=title)
            outcome, actual_path = self.writer.write(
                path=base, data=payload, sha=str(artifact.get("sha256"))
            )
        except MappingError as exc:
            # Symlink on the path, unwritable component, suffix collision:
            # operator-visible error; Runtime naks for redelivery.
            return {"kind": "error", "detail": f"vault write refused: {exc}"}

        render_outcome = None
        render_actual = None
        if render_payload is not None:
            try:
                render_path = self.writer.destination(
                    slug=slug, date_folder=date_folder, title=title,
                    render=True, render_media_type=render_media,
                )
                render_outcome, render_actual = self.writer.write(
                    path=render_path, data=render_payload, sha=render_sha or ""
                )
            except MappingError as exc:
                return {"kind": "error", "detail": f"vault render write refused: {exc}"}

        # Record the ACTUAL chosen path (post-suffix) for this event.
        self.state.record(
            {
                "event_id": event_id,
                "artifact_id": str(artifact.get("artifact_id")),
                "revision": int(artifact.get("revision") or 1),
                "project_slug": slug,
                "vault_path": str(actual_path),
                "projected_at": _now(),
            }
        )
        detail = f"{outcome} at {actual_path.relative_to(self.writer.root)}"
        if render_outcome:
            detail += f" (render {render_outcome} at {render_actual.relative_to(self.writer.root)})"
        return {"kind": "projected", "detail": detail, "write": outcome}


class Runtime:
    def __init__(self, projector: Projector):
        self.projector = projector
        self._nc: Any = None
        self._js: Any = None
        self._stop = asyncio.Event()

    async def connect(self, nats_url: str, durable: str, *, backfill: bool = False) -> None:
        self._nc = await nats.connect(
            nats_url,
            name=f"bloodbank-{durable}",
            connect_timeout=5,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
        )
        self._js = self._nc.jetstream()
        try:
            await self._js.consumer_info(EVENTS_STREAM, durable)
            existing = True
        except Exception:  # noqa: BLE001
            existing = False
        config = None
        if not existing:
            config = ConsumerConfig(
                durable_name=durable,
                description="Project artifact vault projector",
                deliver_policy=DeliverPolicy.ALL if backfill else DeliverPolicy.NEW,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=30,
                max_deliver=10,
                filter_subject=PROJECTOR_SUBJECT,
                max_waiting=1,
                max_ack_pending=32,
            )
        self._sub = await self._js.pull_subscribe(
            PROJECTOR_SUBJECT, durable=durable, stream=EVENTS_STREAM, config=config
        )

    async def _handle(self, msg: Msg) -> None:
        try:
            envelope = json.loads(msg.data.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("malformed message: %s; acking (undecodable)", exc)
            await msg.ack()
            return
        if not isinstance(envelope, dict):
            await msg.ack()
            return
        if msg.subject != PROJECTOR_SUBJECT or envelope.get("type") != ARTIFACT_TYPE:
            log.warning("subject/type mismatch on %s: %r; acking foreign", msg.subject, envelope.get("type"))
            await msg.ack()
            return
        try:
            result = self.projector.handle(envelope)
        except Exception as exc:  # noqa: BLE001 - per-message containment
            log.exception("projection crashed for %s; nak", envelope.get("id"))
            await msg.nak(delay=5)
            return
        log.info("projection kind=%s :: %s", result["kind"], result["detail"])
        # held (unmapped) and deadletter (malformed) are durable terminal
        # states -> ack (their outcome is durably recorded in the holds
        # table with an explicit CLI). errors (missing blob, digest
        # mismatch, transient store failure) nak for redelivery — they
        # survive idle periods and restarts; JetStream max_deliver stops
        # the rotation, but unlike a DLQ fantasy it does NOT move the
        # message anywhere: after exhaustion the operator reads --status
        # (nonzero exit) and --holds, which is why both exist.
        if result["kind"] == "error":
            await msg.nak(delay=5)
            return
        await msg.ack()

    async def run(self, *, once: bool = False) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)
        idle_since: float | None = None
        while not self._stop.is_set():
            try:
                msgs = await self._sub.fetch(batch=8, timeout=2)
            except TimeoutError:
                if once and idle_since is not None and (time.monotonic() - idle_since) >= 4:
                    # --once: one full idle pass over the pending backlog, then exit.
                    return
                idle_since = idle_since if idle_since is not None else time.monotonic()
                continue
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                log.warning("fetch failed: %s: %s", type(exc).__name__, exc)
                await asyncio.sleep(2)
                continue
            idle_since = None
            for msg in msgs:
                await self._handle(msg)


def _default_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "artifact-vault-projector"


async def amain(args: argparse.Namespace) -> int:
    try:
        mapping = load_mapping(args.mapping or _env("ARTIFACT_VAULT_MAPPING", ""))
    except MappingError as exc:
        print(f"mapping error: {exc}", file=sys.stderr)
        return 2
    vault_root = Path(args.vault_root or _env("ARTIFACT_VAULT_ROOT", ""))
    if not vault_root or str(vault_root) == ".":
        print("vault root is required (--vault-root or ARTIFACT_VAULT_ROOT)", file=sys.stderr)
        return 2
    state = ProjectorState(Path(_env("ARTIFACT_VAULT_STATE_DB", str(_default_state_dir() / "state.sqlite3"))))
    try:
        writer = VaultWriter(vault_root, mapping)
        store_root = Path(_env("ARTIFACT_VAULT_STORE", str(_default_state_dir() / "../artifact-sidecar/store")))
        tz = _env("ARTIFACT_VAULT_TZ", "America/New_York")
        projector = Projector(state=state, writer=writer, store_root=store_root, tz=tz)

        if args.holds:
            print(json.dumps(state.holds(include_resolved=args.all), indent=2))
            return 0
        if args.hold_resolve_map:
            slug, vault_dir = args.hold_resolve_map
            mapping_path = Path(args.mapping or _env("ARTIFACT_VAULT_MAPPING", ""))
            if not str(mapping_path):
                print("mapping file path required to record a mapping", file=sys.stderr)
                return 2
            current: dict[str, str] = {}
            if mapping_path.exists():
                current = {
                    k: v
                    for k, v in json.loads(mapping_path.read_text()).items()
                    if not str(k).startswith("$")
                }
            current[slug] = vault_dir
            mapping_path.parent.mkdir(parents=True, exist_ok=True)
            mapping_path.write_text(json.dumps(current, indent=2) + "\n")
            writer.mapping = load_mapping(str(mapping_path))
            resolved = 0
            for hold in state.holds():
                if hold["kind"] == "unmapped" and json.loads(hold.get("envelope") or "{}").get("slug") == slug:
                    state.resolve_hold(hold["hold_id"], {"mapped_to": vault_dir})
                    resolved += 1
            print(json.dumps({"mapping_file": str(mapping_path), "resolved_holds": resolved}))
            return 0
        if args.status:
            holds = state.holds()
            print(json.dumps({
                "vault_root": str(writer.root),
                "mapping": writer.mapping,
                "projections": state.counts(),
                "unresolved_holds": len(holds),
                "unresolved_hold_kinds": sorted({h["kind"] for h in holds}),
            }, indent=2))
            return 1 if holds else 0
        if args.plan:
            print(json.dumps({
                "vault_root": str(writer.root),
                "mapping": writer.mapping,
                "timezone": tz,
                "rules": [
                    "full canonical schema+invariants validated before any write",
                    "date folder = origin.created_at (source event time) in " + tz,
                    "explicit mapping only; unmapped slug -> durable hold, no shadow dirs",
                    "malformed envelope -> durable deadletter hold",
                    "existing files never overwritten; deterministic -<sha8> suffix; actual path recorded",
                    "source/render digests+lengths verified pre-write; render ext from its own media type",
                    "dirfd+O_NOFOLLOW traversal; O_EXCL tmp + no-clobber hard link; dir fsync",
                ],
            }, indent=2))
            return 0

        nats_url = _env("ARTIFACT_VAULT_NATS_URL", "nats://127.0.0.1:4222")
        durable = _env("ARTIFACT_VAULT_DURABLE", "bloodbank-artifact-vault-projector")
        backfill = _env("ARTIFACT_VAULT_BACKFILL", "") == "1"
        runtime = Runtime(projector)
        log.info("connecting %s durable=%s subject=%s", nats_url, durable, PROJECTOR_SUBJECT)
        await runtime.connect(nats_url, durable, backfill=backfill)
        log.info("subscribed; projecting")
        await runtime.run(once=args.once)
        return 0
    finally:
        state.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Project bloodbank.project.artifact.created into the vault"
    )
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--holds", action="store_true", help="list unresolved holds/deadletters")
    parser.add_argument("--all", action="store_true", help="include resolved holds")
    parser.add_argument("--hold-resolve-map", nargs=2, metavar=("SLUG", "VAULT_DIR"))
    parser.add_argument("--once", action="store_true", help="drain the pending backlog, then exit")
    parser.add_argument("--mapping", help="path to explicit slug->dir mapping JSON")
    parser.add_argument("--vault-root", help="vault root directory")
    args = parser.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
