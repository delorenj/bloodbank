"""Shared library for the project-artifact sidecar and the vault projector.

Two bus participants, one contract:

  artifact-sidecar   consumes agent events (bloodbank.evt.agent.tool.completed,
                     bloodbank.evt.conversation.turn.completed), captures
                     verified artifact bytes into a content-addressed store,
                     resolves the project through the canonical pjangler
                     registry, and publishes
                     bloodbank.project.artifact.created.

  artifact-vault-projector  consumes bloodbank.evt.project.artifact.created
                     and copies the bytes into the configured canonical
                     project vault path.

Stdlib-only per the repo's services constraint (Python 3.11+ for zoneinfo;
developed and tested on 3.14). Security posture, both processes:

  - No execution of incoming content. Ever. Nothing here calls eval or exec
    or renders anything; a captured mermaid source is stored as text and
    copied as text. The ONLY subprocess is `git rev-parse` for repository
    identity attribution (fixed argv, timeout, no shell, never on content).
  - Local reads are restricted to explicitly configured allowlisted project
    roots / output directories. Nothing else is opened.
  - Path safety: candidate paths are checked lexically component-by-component
    with lstat (a symlink anywhere below the root is refused BEFORE
    resolution, so a within-root symlink cannot hide a hop), then containment
    is verified with os.path.commonpath on fully resolved paths (not string
    prefix). `..` never survives.
  - Content-addressed store: refs are strict lowercase-hex sha256 only;
    traversal-shaped or mixed-case refs are refused. Every store walk uses
    directory fds with O_NOFOLLOW; reads are size-bounded; preexisting blobs
    are digest-verified (a corrupt blob is refused, never trusted); writes
    are link-based no-clobber with directory fsync; store dirs 0700, blobs
    0600; state dbs are chmod 0600.
  - Size bounds: captures above MAX_CAPTURE_BYTES are refused, not truncated.
  - No secret/cache/config scraping: only explicit artifact output locations
    qualify; ~/.ssh, ~/.config, ~/.cache, .env, config/, secrets/, scratch/
    and friends are excluded by the allowlist plus a defensive denylist of
    path fragments applied to EVERY capture (inline or file) before
    persistence.
  - No network fetches of any kind in the capture path. The only outbound
    traffic is the bus itself (NATS publish/subscribe) and, optionally, the
    read-only pjangler registry HTTP endpoint on loopback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_module
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Event identity
# ---------------------------------------------------------------------------

ARTIFACT_TYPE = "bloodbank.project.artifact.created"
ARTIFACT_SUBJECT = "bloodbank.evt.project.artifact.created"
SOURCE_EVENT_TYPES = (
    "bloodbank.agent.tool.completed",
    "bloodbank.conversation.turn.completed",
)

# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

MAX_CAPTURE_BYTES = 64 * 1024 * 1024  # schema caps artifact.bytes at 64 MiB
DEFAULT_MAX_REPLY_CHARS = 200_000  # cap on reply_text scanned for mermaid

# Defensive denylist applied to every candidate path (inline logical paths
# included — the path is prepended with "/" so repo-relative fragments match)
# BEFORE any allowlist logic: even if an operator misconfigures an allowlist
# root, these fragments are never persisted as artifacts. This is not the
# primary control (explicit qualification is); it is the seatbelt under it.
DENIED_PATH_FRAGMENTS = (
    "/.ssh/", "/.gnupg/", "/.aws/", "/.config/", "/.cache/", "/.local/share/",
    "/.hermes/", "/.claude/", "/.codex/", "/.agents/",
    "/.env", "/.git/", "/node_modules/", "/__pycache__/",
    "/config/", "/secrets/", "/scratch/",
)

# ---------------------------------------------------------------------------
# Media types the adapters may attach. Detection is by extension only; the
# content is never executed and never sniffed by loading it anywhere.
# ---------------------------------------------------------------------------

EXTENSION_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".mermaid": "text/vnd.mermaid",
    ".mmd": "text/vnd.mermaid",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".csv": "text/csv",
    ".geojson": "application/geo+json",
    ".drawio": "application/vnd.jgraph.drawio",
    ".excalidraw": "application/vnd.excalidraw+json",
    ".ipynb": "application/x-ipynb+json",
}

# Media types whose bytes can arrive honestly as an inline UTF-8 string.
# Inline tool arguments carry a STRING; a string can never be a valid PNG/JPEG/
# GIF/WebP/PDF, so those extensions never qualify from inline content — no
# fabricated binaries. Binary media DOES qualify when the bytes are read from
# an explicitly delivered local file (discovery MEDIA adapter reads genuine
# bytes through read_file_bounded, never trusts an inline string).
TEXT_INLINE_MEDIA_TYPES = frozenset({
    "text/markdown", "text/plain", "text/vnd.mermaid", "text/html",
    "application/json", "text/csv", "application/geo+json",
    "application/vnd.jgraph.drawio", "application/vnd.excalidraw+json",
    "application/x-ipynb+json", "image/svg+xml",
})

# Media types a DELIVERED LOCAL FILE may carry (extension-gated, real bytes
# read from disk under the capture read policy). Binary types are here and
# only here: genuine bytes, no inline-string fabrication path.
DELIVERED_FILE_MEDIA_TYPES = frozenset(EXTENSION_MEDIA_TYPES.values())

# Preferred extension per media type (render twin naming, projector writes).
MEDIA_TYPE_EXTENSIONS = {
    "text/markdown": ".md",
    "text/plain": ".txt",
    "text/vnd.mermaid": ".mmd",
    "text/html": ".html",
    "application/json": ".json",
    "text/csv": ".csv",
    "application/geo+json": ".geojson",
    "application/vnd.jgraph.drawio": ".drawio",
    "application/vnd.excalidraw+json": ".excalidraw",
    "application/x-ipynb+json": ".ipynb",
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


def media_type_for(name: str) -> str | None:
    """Media type for a filename by extension, or None if unsupported."""
    lower = name.lower()
    for ext, media in EXTENSION_MEDIA_TYPES.items():
        if lower.endswith(ext):
            return media
    return None


def denied_path(path_text: str) -> str | None:
    """The first denied fragment matched by this path text, or None.

    The candidate is normalized to a POSIX path and prefixed with "/" so
    repo-relative forms (".env", "config/app.json", "scratch/notes") match
    the same fragment table as absolute paths.
    """
    text = "/" + str(path_text).replace("\\", "/").lstrip("/")
    for fragment in DENIED_PATH_FRAGMENTS:
        if fragment in text:
            return fragment
    return None


# ---------------------------------------------------------------------------
# Content-addressed store
# ---------------------------------------------------------------------------


class StoreError(RuntimeError):
    """Raised when the content-addressed store cannot satisfy a request."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def store_path_for(root: Path, sha256: str) -> Path:
    """Fan-out path for a digest: root/ab/abcdef... (lexical only)."""
    return root / sha256[:2] / sha256


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _open_dir_nofollow(name: str, dir_fd: int) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def _read_fd_bounded(fd: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise StoreError(f"blob exceeds maximum readable size {max_bytes}")
        chunks.append(chunk)
    return b"".join(chunks)


def put_blob(root: Path, data: bytes) -> tuple[str, Path]:
    """Write bytes into the content-addressed store; return (sha256, path).

    Safety contract:
      - The store root and fan-out directory are opened through directory
        fds with O_NOFOLLOW: a symlinked root or fan-out dir is refused.
      - A preexisting blob is digest-verified: corrupt bytes already at the
        target are a StoreError (never trusted, never overwritten).
      - New blobs land via O_EXCL tmp + fsync + atomic hard link (no-clobber
        under concurrency), then the directory is fsynced.
      - Private permissions: fan-out dirs 0700, blobs 0600.
    """
    if len(data) > MAX_CAPTURE_BYTES:
        raise StoreError(f"blob larger than MAX_CAPTURE_BYTES ({MAX_CAPTURE_BYTES})")
    digest = sha256_bytes(data)
    try:
        root_fd = _open_dir_nofollow(str(root), os.open(".", os.O_RDONLY))
    except OSError as exc:
        raise StoreError(f"store root {root} not openable (symlink or missing?): {exc}") from exc
    try:
        fan = digest[:2]
        try:
            fan_fd = _open_dir_nofollow(fan, root_fd)
        except FileNotFoundError:
            try:
                os.mkdir(fan, 0o700, dir_fd=root_fd)
                os.fsync(root_fd)
                fan_fd = _open_dir_nofollow(fan, root_fd)
            except OSError as exc:
                raise StoreError(f"store fan-out {fan!r} unusable: {exc}") from exc
        except OSError as exc:
            raise StoreError(f"store fan-out {fan!r} refused (symlink?): {exc}") from exc
        try:
            existing = _verify_existing_blob(fan_fd, digest)
            if existing == data:
                return digest, store_path_for(root, digest)
            if existing is not None:
                raise StoreError(
                    f"store blob {digest} exists with different bytes; refusing "
                    "(corrupt preexisting blob is never trusted)"
                )
            tmp = f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fan_fd)
            try:
                os.write(tfd, data)
                os.fsync(tfd)
            finally:
                os.close(tfd)
            try:
                try:
                    os.link(tmp, digest, src_dir_fd=fan_fd, dst_dir_fd=fan_fd)
                    os.fsync(fan_fd)
                except FileExistsError:
                    # Lost a race: verify the winner's bytes are ours.
                    if _verify_existing_blob(fan_fd, digest) != data:
                        raise StoreError(
                            f"store blob {digest} raced to different bytes; refusing"
                        )
            finally:
                try:
                    os.unlink(tmp, dir_fd=fan_fd)
                except OSError:
                    pass
            return digest, store_path_for(root, digest)
        finally:
            os.close(fan_fd)
    finally:
        os.close(root_fd)


def _verify_existing_blob(fan_fd: int, digest: str) -> bytes | None:
    """Bytes of an existing blob (verified against digest), or None if absent."""
    try:
        fd = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fan_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        # ELOOP (symlink), EACCES, … — never readable, never trusted.
        raise StoreError(f"store entry {digest} refused: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            raise StoreError(f"store entry {digest} is not a regular file")
        if st.st_size > MAX_CAPTURE_BYTES:
            raise StoreError(f"store blob {digest} exceeds size bound")
        data = _read_fd_bounded(fd, MAX_CAPTURE_BYTES)
        if sha256_bytes(data) != digest:
            raise StoreError(f"store blob {digest} content does not match its digest")
        return data
    finally:
        os.close(fd)


def get_blob(root: Path, ref: str) -> bytes:
    """Read bytes for a 'sha256:<hex>' ref, verifying the digest on read.

    The ref must be exactly 'sha256:' + 64 STRICT LOWERCASE hex characters:
    traversal shapes ('..', '/', uppercase, non-hex) are refused before any
    filesystem operation. The walk uses directory fds with O_NOFOLLOW, the
    final entry must be a regular file, the read is size-bounded, and the
    digest is verified.
    """
    if not isinstance(ref, str) or not ref.startswith("sha256:") or len(ref) != 7 + 64:
        raise StoreError(f"malformed content ref {ref!r}")
    digest = ref[len("sha256:"):]
    if not _SHA256_RE.match(digest):
        raise StoreError(f"content ref digest must be 64 lowercase hex chars, got {ref!r}")
    try:
        root_fd = _open_dir_nofollow(str(root), os.open(".", os.O_RDONLY))
    except OSError as exc:
        raise StoreError(f"store root {root} not openable: {exc}") from exc
    try:
        try:
            fan_fd = _open_dir_nofollow(digest[:2], root_fd)
        except OSError as exc:
            raise StoreError(f"blob {ref} not readable in store {root}: {exc}") from exc
        try:
            data = _verify_existing_blob(fan_fd, digest)
        finally:
            os.close(fan_fd)
    finally:
        os.close(root_fd)
    if data is None:
        raise StoreError(f"blob {ref} not present in store {root}")
    return data


def ensure_store_root(root: Path) -> Path:
    """Validate a store root: real directory (or creatable), never a symlink."""
    if root.is_symlink():
        raise StoreError(f"store root {root} is a symlink; refusing")
    if not root.exists():
        os.makedirs(root, mode=0o700, exist_ok=True)
    if not root.is_dir():
        raise StoreError(f"store root {root} is not a directory")
    return root


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


class PathViolation(ValueError):
    """A candidate path violated the read policy."""


def safe_resolve_under_root(candidate: Path, root: Path, *, label: str) -> Path:
    """Resolve candidate under an allowlist root, refusing escapes and symlinks.

    Two independent checks, in this order:

    1. LEXICAL symlink check on the ORIGINAL components: every component of
       the normalized candidate below the root is lstat'ed BEFORE any
       resolution. Resolving first would hide a within-root symlink (the
       resolved path contains no symlink anymore), so the check must run on
       the original components.
    2. Containment on fully resolved paths via os.path.commonpath (a string
       prefix check would accept /root-evil under root).
    """
    root_real = root.resolve(strict=False)
    # Normalize lexically so '..' never survives, then check every component
    # below the root against symlinks using the ORIGINAL (non-resolved) path.
    normalized = Path(os.path.normpath(str(candidate)))
    probe = normalized if normalized.is_absolute() else Path.cwd() / normalized
    current = Path(probe.anchor)
    for part in probe.parts[1 if probe.anchor else 0:]:
        if not part:
            continue
        current = current / part
        try:
            st = os.lstat(current)
        except OSError:
            break  # nonexistent tail components cannot be symlinks
        if stat_module.S_ISLNK(st.st_mode):
            # A symlink component that is part of the REAL root's own path is
            # how the operator spelled the root (e.g. a symlinked prefix);
            # anything else — including symlinks fully inside the root — is a
            # refused hop.
            if str(current) not in str(root_real) and current != root_real:
                raise PathViolation(
                    f"{label}: {candidate} traverses symlink component {current}"
                )
    resolved = candidate.resolve(strict=False)
    try:
        common = os.path.commonpath((str(root_real), str(resolved)))
    except ValueError as exc:  # different drives / null bytes
        raise PathViolation(f"{label}: cannot compare {candidate} with root {root}") from exc
    if common != str(root_real):
        raise PathViolation(f"{label}: {candidate} escapes allowlist root {root}")
    return resolved


def assert_readable_capture_path(path: Path, roots: tuple[Path, ...]) -> Path:
    """Full read policy for a capture source path.

    Order matters: denylist first (so a misconfigured allowlist cannot grant
    secrets), then allowlist containment + symlink refusal.
    """
    text = str(path)
    for fragment in DENIED_PATH_FRAGMENTS:
        if fragment in text:
            raise PathViolation(f"capture path {path} matches denied fragment {fragment!r}")
    for root in roots:
        try:
            return safe_resolve_under_root(path, root, label="capture")
        except PathViolation:
            continue
    raise PathViolation(
        f"capture path {path} is not inside any allowlisted root {list(map(str, roots))}"
    )


# ---------------------------------------------------------------------------
# Bounded local file reads (delivered attachments only)
# ---------------------------------------------------------------------------


def read_file_bounded(
    path: Path,
    roots: tuple[Path, ...],
    *,
    max_bytes: int = MAX_CAPTURE_BYTES,
) -> tuple[bytes, str]:
    """Read GENUINE bytes of an explicitly delivered local file.

    Full policy chain, in order (fail loud, never partial):
      1. denylist fragments (secrets/config/scratch/…) — before any allowlist;
      2. allowlist containment + lexical symlink refusal + resolved
         containment via assert_readable_capture_path;
      3. open O_NOFOLLOW on the FINAL component too, regular-file check;
      4. read bounded by max_bytes (oversize is refused, never truncated);
      5. digest the actual bytes and return them (never a size claim).

    Returns (data, sha256). Raises PathViolation/StoreError/OSError.
    """
    resolved = assert_readable_capture_path(path, roots)
    import os as _os

    try:
        fd = _os.open(resolved, _os.O_RDONLY | _os.O_NOFOLLOW)
    except OSError as exc:
        raise StoreError(f"delivered file {resolved.name} refused: {exc}") from exc
    try:
        st = _os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            raise StoreError(f"delivered file {resolved.name} is not a regular file")
        if st.st_size > max_bytes:
            raise StoreError(
                f"delivered file {resolved.name} exceeds size bound {max_bytes}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = _os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise StoreError(
                    f"delivered file {resolved.name} exceeds size bound {max_bytes}"
                )
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        _os.close(fd)
    return data, sha256_bytes(data)


# ---------------------------------------------------------------------------
# Project registry (canonical pjangler registry, read-only)
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


class RegistryError(RuntimeError):
    """The pjangler registry could not answer authoritatively."""


@dataclass(frozen=True)
class ProjectRef:
    slug: str
    name: str


class ProjectRegistry:
    """Read-only view of the canonical pjangler project registry.

    The registry maps project_id -> entry (name, slug, repo_path, status).
    The live endpoint is http://127.0.0.1:8764/v1/registry. An explicit file
    path (a checked-out registry snapshot) is also supported so tests and
    offline runs never need the live service.
    """

    def __init__(self, projects: dict[str, dict[str, Any]], status: dict[str, Any] | None = None):
        self._projects = projects
        self._status = status or {}

    @classmethod
    def from_http(cls, url: str, timeout: float = 5.0) -> "ProjectRegistry":
        import urllib.request

        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - registry unreachable is one failure
            raise RegistryError(f"registry fetch failed from {url}: {exc}") from exc
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: Any) -> "ProjectRegistry":
        if not isinstance(payload, dict):
            raise RegistryError("registry payload must be an object")
        if payload.get("schema_version") != 1:
            raise RegistryError(
                f"registry schema_version must be 1, got {payload.get('schema_version')!r}"
            )
        projects = payload.get("projects")
        if not isinstance(projects, dict) or not projects:
            raise RegistryError("registry payload.projects must be a non-empty object")
        status = payload.get("__registry_status")
        if status is not None and not isinstance(status, dict):
            raise RegistryError("registry __registry_status must be an object when present")
        for project_id, entry in projects.items():
            if not isinstance(project_id, str) or not project_id:
                raise RegistryError("registry project_id keys must be non-empty strings")
            if not isinstance(entry, dict):
                raise RegistryError(f"registry entry {project_id!r} must be an object")
            slug = entry.get("slug")
            if not isinstance(slug, str) or not _SLUG_RE.match(slug):
                raise RegistryError(
                    f"registry entry {project_id!r} has invalid slug {slug!r}"
                )
            repo_path = entry.get("repo_path")
            if not isinstance(repo_path, str) or not repo_path.startswith("/"):
                raise RegistryError(
                    f"registry entry {project_id!r} has invalid repo_path {repo_path!r}"
                )
            if slug != project_id and projects.get(slug) is not None and slug != project_id:
                # slug colliding with a DIFFERENT project_id is ambiguous
                other = projects[slug]
                if isinstance(other, dict) and other.get("slug") != slug:
                    raise RegistryError(f"registry slug {slug!r} is ambiguous")
        return cls(dict(projects), status)

    def entry(self, project_id: str) -> dict[str, Any] | None:
        entry = self._projects.get(project_id)
        return dict(entry) if isinstance(entry, dict) else None

    def entry_for_slug(self, slug: str) -> dict[str, Any] | None:
        """The registry entry whose slug equals this one, or None."""
        for entry in self._projects.values():
            if isinstance(entry, dict) and entry.get("slug") == slug:
                return dict(entry)
        return None

    def index_status(self, project_id: str) -> dict[str, Any] | None:
        value = self._status.get(project_id)
        return dict(value) if isinstance(value, dict) else None

    def resolve_by_repo_path(self, cwd: str) -> ProjectRef | None:
        """Resolve a working directory to a project by repo_path containment.

        Returns None when no indexed-ok registry entry's repo_path contains
        the directory. A project with a failing index status is skipped: its
        manifest is stale, and stale metadata must not decide attribution.
        (A 'planned' status is fine — planning IS project work; the index
        status, not the lifecycle status, is what gates trust.)
        """
        try:
            real = Path(cwd).resolve(strict=False)
        except OSError:
            return None
        best: tuple[int, ProjectRef] | None = None
        for project_id, entry in self._projects.items():
            if not isinstance(entry, dict):
                continue
            status = self.index_status(project_id)
            if status is not None and status.get("status") != "ok":
                continue
            repo_path = entry.get("repo_path")
            if not isinstance(repo_path, str) or not repo_path:
                continue
            try:
                repo_real = Path(repo_path).resolve(strict=False)
                common = os.path.commonpath((str(repo_real), str(real)))
            except (OSError, ValueError):
                continue
            if common != str(repo_real):
                continue
            depth = len(real.parts) - len(repo_real.parts)
            if depth < 0:
                continue
            ref = ProjectRef(slug=str(entry.get("slug") or project_id), name=str(entry.get("name") or project_id))
            if best is None or depth < best[0]:
                best = (depth, ref)
        return best[1] if best else None

    def validate_event_project(self, project: dict[str, Any]) -> None:
        """A published artifact event's project must match the registry.

        Raises RegistryError when slug/name are inconsistent with the
        canonical entry for that slug. Consistency is by slug: the registry
        keys entries by project_id, and the published slug must equal the
        canonical entry's slug for whichever entry claims it.
        """
        slug = project.get("slug")
        if not isinstance(slug, str) or not slug:
            raise RegistryError("artifact project.slug missing")
        for project_id, entry in self._projects.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("slug") == slug:
                name = project.get("name")
                if isinstance(name, str) and name != entry.get("name"):
                    raise RegistryError(
                        f"artifact project.name {name!r} disagrees with registry "
                        f"entry {project_id} name {entry.get('name')!r}"
                    )
                return
        raise RegistryError(f"artifact project slug {slug!r} is not in the registry")


# ---------------------------------------------------------------------------
# Git repository identity (worktree-safe attribution)
# ---------------------------------------------------------------------------


def git_repo_identity(wd: str) -> tuple[str, str] | None:
    """(toplevel, git_common_dir) real paths for a working directory, or None.

    Fixed argv, no shell, bounded timeout — this is our own identity probe,
    never execution of incoming content. The common dir is what identifies a
    linked worktree's PARENT repository: a worktree of the bloodbank repo
    reports bloodbank's .git here even though the worktree path itself is
    outside the bloodbank checkout.
    """
    try:
        out = subprocess.run(
            ["git", "-C", wd, "rev-parse", "--show-toplevel", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    top = Path(lines[0])
    common = Path(lines[1])
    if not common.is_absolute():
        common = Path(top) / common
    try:
        return str(top.resolve(strict=False)), str(common.resolve(strict=False))
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Artifact identity
# ---------------------------------------------------------------------------

# uuid5 namespace for artifact identity. Deriving artifact_id from
# (project slug, logical path) makes the same logical file written twice in
# one session (or across sessions) map to the same id, so revisions chain.
ARTIFACT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:33god:project-artifact:v1")


def artifact_id_for(slug: str, logical_path: str) -> str:
    return uuid.uuid5(ARTIFACT_NAMESPACE, f"{slug}:{logical_path}").hex


def logical_path_for(absolute_path: str, project_root: str) -> str:
    """Project-relative posix path for an absolute path under a project root."""
    rel = os.path.relpath(absolute_path, project_root)
    return Path(rel).as_posix()


# ---------------------------------------------------------------------------
# Held captures (missing attribution)
# ---------------------------------------------------------------------------

_HOLD_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def hold_id_for(source_event_id: str, logical_path: str, sha256: str) -> str:
    """Stable id for a held capture: identity includes the SOURCE event and
    the candidate logical path, not just the digest — the same bytes written
    by two different sessions (or two projects) are two distinct holds, each
    keeping its own provenance."""
    return hashlib.sha256(
        f"{source_event_id}|{logical_path}|{sha256}".encode("utf-8")
    ).hexdigest()[:32]


def is_hold_id(value: str) -> bool:
    return isinstance(value, str) and bool(_HOLD_ID_RE.match(value))


def hold_record(
    *,
    reason: str,
    source_event_id: str,
    source_event_type: str,
    source_event_time: str,
    working_directory: str | None,
    title: str,
    sha256: str,
    bytes_len: int,
    media_type: str,
    ref: str,
    logical_path: str = "",
) -> dict[str, Any]:
    """A durable hold record: attribution was ambiguous, bytes are preserved.

    Held captures are the honest alternative to guessing a project. The CLI
    lists them; an operator resolves one with its hold_id (the resolved
    project slug is recorded and the event can then be published with basis
    operator_resolution).
    """
    return {
        "hold_id": hold_id_for(source_event_id, logical_path, sha256),
        "reason": reason,
        "held_at": None,  # stamped by the store on write
        "source_event": {
            "type": source_event_type,
            "id": source_event_id,
            "time": source_event_time,
        },
        "working_directory": working_directory,
        "title": title,
        "logical_path": logical_path,
        "sha256": sha256,
        "bytes": bytes_len,
        "media_type": media_type,
        "ref": ref,
        "resolution": None,
    }
