"""Deterministic artifact discovery adapters.

Each adapter inspects one REAL, already-published bloodbank event shape and
either qualifies a capture (returning a Candidate) or returns None. There is
deliberately no fuzzy content inference here: qualification is a positive
structural match on the event's own fields, and everything that does not
match is left alone (or held, when bytes were preserved but attribution was
ambiguous).

Supported bases (docs/event-naming.md §11.4, artifact):

  agent_tool_write       bloodbank.agent.tool.completed whose data.arguments
                         is an object carrying BOTH a file-path-ish argument
                         (file_path | path | notebook_path) and FULL inline
                         content (content | file_text), PLUS an explicit
                         user-facing signal: a producer delivery marker
                         (data.delivered / data.user_facing truthy, or
                         non-empty data.deliverables / data.attachments) or a
                         path inside a project-configured producer output
                         tree (default `_bmad_output/` + `_bmad-output/`
                         planning outputs, evidence/logs/scratch/tmp
                         excluded). A generic README/config/docs write with
                         no signal is process, not a deliverable.

  conversation_reply_mermaid  ONE fenced ```mermaid block in
                         bloodbank.conversation.turn.completed
                         data.reply_text, attributed per thread+turn and
                         named by a digest of the block itself: two turns
                         discussing the same diagram in one thread are the
                         SAME artifact (revision chain); the same diagram in
                         a different thread is a DIFFERENT artifact.

  conversation_reply_delivered   reply text or explicitly delivered LOCAL
                         files, ONLY when the producer marked the event
                         user-facing (`data.delivered`/`data.user_facing`
                         truthy, or non-empty `data.deliverables`/
                         `data.attachments`). Delivered local files arrive as
                         absolute paths (`MEDIA:<path>` tokens in reply_text
                         or `path`/`file` entries in deliverables) and are
                         read under the strict capture policy: denylist,
                         operator-allowlisted output roots
                         (ARTIFACT_SIDECAR_CAPTURE_ROOTS), lexical symlink
                         refusal, O_NOFOLLOW, size-bounded, digest from the
                         actual bytes. THIS is the only path to genuine
                         binary artifacts (PNG/PDF) — real bytes from disk,
                         never an inline string. No arbitrary remote fetch;
                         the .hermes cache is deliberately not readable.

  project_activity_report  a bloodbank.project.activity.recorded event: the
                         report markdown (canonical) + its self-contained HTML
                         (render twin), attributed by the event's own
                         data.project.slug (registry-checked), identified per
                         project + audience + window.start.

  operator_resolution    an operator mapped a previously held capture to a
                         project (sidecar.publish_resolution path).

Inline content policy: tool arguments carry a STRING. A string can honestly
be text (markdown, mermaid, svg, json …) but never a valid PNG/JPEG/GIF/
WebP/PDF, so those binary extensions never qualify from inline content — no
fabricated binaries. Binary media qualifies only through the delivered-file
adapter above, whose bytes are read from disk.

Anything else — Bash outputs, edit diffs, read results, structured subagent
reports, ambient repo docs, config files — is intentionally not an artifact.
Those are process, not deliverables.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from artifact_lib import (
    DEFAULT_MAX_REPLY_CHARS,
    DELIVERED_FILE_MEDIA_TYPES,
    MAX_CAPTURE_BYTES,
    PathViolation,
    StoreError,
    TEXT_INLINE_MEDIA_TYPES,
    denied_path,
    media_type_for,
    read_file_bounded,
)

WRITE_PATH_ARGUMENTS = ("file_path", "path", "notebook_path")
WRITE_CONTENT_ARGUMENTS = ("content", "file_text")

# Tools whose completed calls plausibly carry a full-file write. The adapter
# matches on structure (path + inline full content), and the tool-name gate
# keeps a read tool with echoed arguments from qualifying. Edit-family tools
# (old/new string pairs, no full content) never qualify on structure, so they
# are not listed.
WRITE_TOOL_NAMES = {"write", "writetool", "createfile", "writefile"}

# ---------------------------------------------------------------------------
# Explicit user-facing qualification (the delivery gate)
# ---------------------------------------------------------------------------
#
# A write tool call with path + inline content is NOT by itself a deliverable:
# agents write READMEs, configs and scratch docs constantly, and "the file is
# markdown" never made content user-facing. A write qualifies only through an
# EXPLICIT signal:
#
#   1. the producer marked the event delivered/user-facing
#      (data.delivered / data.user_facing truthy, or non-empty
#      data.deliverables / data.attachments), OR
#   2. the path sits inside a per-project CONFIGURED producer output tree —
#      by default the BMAD planning outputs, both spellings actually seen in
#      this fleet (`_bmad_output/` in Bloodbank, `_bmad-output/` elsewhere).
#
# Process byproducts under those trees (evidence/, logs/, scratch/, tmp/) are
# excluded even when an operator widens the glob list: they are workflow
# exhaust, not deliverables, and the hold/resolution path exists for the rare
# genuine exception.

PRODUCER_OUTPUT_EXTENSIONS = (
    ".md", ".markdown", ".txt", ".json", ".csv", ".geojson", ".html",
    ".svg", ".mmd", ".mermaid", ".drawio", ".excalidraw",
)

DEFAULT_PRODUCER_OUTPUT_GLOBS = tuple(
    f"{root}**/*{ext}"
    for root in ("_bmad_output/", "_bmad-output/")
    for ext in PRODUCER_OUTPUT_EXTENSIONS
)

# Hard floor under the glob allowlist: these path components never qualify as
# producer outputs, whatever the configuration says.
PRODUCER_OUTPUT_EXCLUDED_PARTS = ("evidence", "logs", "log", "scratch", "tmp")

_DELIVERY_MARKER_LIST_KEYS = ("deliverables", "attachments")


def _tool_family(tool_name: str) -> str:
    """Normalized tool family: lowercase, punctuation/space stripped.

    Hermes names its writer `write_file`, Claude Code `Write`, opencode
    `write`: `write_file` -> `writefile`, `*Write` -> `write`. Without the
    underscore normalization the single most common producer on this box was
    invisible to the adapter.
    """
    return re.sub(r"[\s*_+-]+", "", (tool_name or "").strip().lower())


def tool_arguments(data: dict[str, Any]) -> dict[str, Any] | None:
    """The tool call's input object, wherever the producer put it.

    Claude Code / opencode / codex events carry it as `data.arguments`. Real
    Hermes `write_file` events carried NO `data.arguments`; the inputs sit at
    `data.payload.tool_input` (the native shell_hooks wire, kept verbatim by
    the adapter). `data.arguments` wins when it is a non-empty object, so a
    producer that promotes the inputs (bloodbank cc10ca6) is read as-is;
    otherwise the native `payload.tool_input` is the fallback. Returns None
    when neither is an object.
    """
    arguments = data.get("arguments")
    if isinstance(arguments, dict) and arguments:
        return arguments
    payload = data.get("payload")
    if isinstance(payload, dict):
        native = payload.get("tool_input")
        if isinstance(native, dict):
            return native
    return arguments if isinstance(arguments, dict) else None


def explicit_delivery_marker(data: dict[str, Any]) -> bool:
    """True when the PRODUCER explicitly marked this event user-facing."""
    if data.get("delivered") or data.get("user_facing"):
        return True
    for key in _DELIVERY_MARKER_LIST_KEYS:
        value = data.get(key)
        if isinstance(value, (list, tuple)) and len(value) > 0:
            return True
    return False


def _fnmatch_posix(pattern: str, rel: str) -> bool:
    import fnmatch

    return fnmatch.fnmatchcase(rel, pattern)


def _project_relative(absolute_path: str, working_directory: str | None) -> str | None:
    """Project-relative POSIX form of a path under the working directory, or None."""
    if not working_directory:
        return None
    try:
        rel = PurePosixPath(str(absolute_path).replace("\\", "/"))
        wd = PurePosixPath(str(working_directory).replace("\\", "/"))
        rel = rel.relative_to(wd)
    except ValueError:
        return None
    return rel.as_posix()


def matches_producer_output(
    absolute_path: str,
    working_directory: str | None,
    globs: tuple[str, ...] = DEFAULT_PRODUCER_OUTPUT_GLOBS,
) -> bool:
    """Is this path inside a project-configured producer output tree?

    Requires a working_directory to anchor the project-relative comparison
    (the cwd itself or any ancestor of it: sessions rooted in a project
    subdirectory still qualify); without it there is no project to be
    relative to, so False. The excluded
    component floor (evidence/logs/scratch/tmp) applies regardless of the
    configured glob list.
    """
    # The agent's cwd is frequently a SUBDIRECTORY of the project root (a
    # Claude session in <repo>/native writing <repo>/_bmad-output/x.md). Try
    # the working directory and each of its ancestors as the anchor; the
    # output-tree glob must match the path relative to that anchor, and the
    # sidecar's repo-root containment check (safe_resolve_under_root) still
    # refuses any path that falls outside the attributed project.
    if not working_directory:
        return False
    wd = PurePosixPath(str(working_directory).replace("\\", "/"))
    for anchor in (wd, *wd.parents):
        if anchor == PurePosixPath("/"):
            break  # never anchor at the filesystem root
        rel = _project_relative(absolute_path, str(anchor))
        if rel is None:
            continue
        if any(part in PRODUCER_OUTPUT_EXCLUDED_PARTS for part in PurePosixPath(rel).parts):
            continue
        if any(_fnmatch_posix(g, rel) for g in globs):
            return True
    return False


def write_with_content_but_refused(envelope: dict[str, Any]) -> bool:
    """True when a write-family tool call carried path + inline content but
    the adapter refused it (unsupported extension, binary-from-inline, or
    denied content location). Drives an explicit 'skipped' disposition so
    refusals are observable, not silent."""
    data = envelope.get("data") or {}
    if not isinstance(data, dict) or data.get("outcome") == "error":
        return False
    tool = str(data.get("tool_name") or "")
    if _tool_family(tool) not in WRITE_TOOL_NAMES:
        return False
    arguments = tool_arguments(data)
    return bool(
        isinstance(arguments, dict)
        and any(isinstance(arguments.get(k), str) and arguments[k] for k in WRITE_PATH_ARGUMENTS)
        and any(isinstance(arguments.get(k), str) for k in WRITE_CONTENT_ARGUMENTS)
    )


def write_like_but_unsupported(envelope: dict[str, Any]) -> bool:
    """True when a write-family tool completed but without full inline content.

    Drives the explicit 'unsupported' disposition so operators can see how
    many candidate writes were NOT captured (reported, not silently widened).
    """
    data = envelope.get("data") or {}
    if not isinstance(data, dict) or data.get("outcome") == "error":
        return False
    tool = str(data.get("tool_name") or "")
    if _tool_family(tool) not in WRITE_TOOL_NAMES:
        return False
    arguments = tool_arguments(data)
    return not (
        isinstance(arguments, dict)
        and any(isinstance(arguments.get(k), str) and arguments[k] for k in WRITE_PATH_ARGUMENTS)
        and any(isinstance(arguments.get(k), str) for k in WRITE_CONTENT_ARGUMENTS)
    )


class Capture:
    """Qualified bytes plus where they came from."""

    __slots__ = ("title", "logical_path", "media_type", "data", "render")

    def __init__(
        self,
        *,
        title: str,
        logical_path: str,
        media_type: str,
        data: bytes,
        render: "Capture | None" = None,
    ):
        self.title = title
        self.logical_path = logical_path
        self.media_type = media_type
        self.data = data
        self.render = render


@dataclass(frozen=True)
class Candidate:
    """A qualified artifact capture awaiting project resolution."""

    basis: str
    qualification_reason: str
    capture: Capture
    working_directory: str | None
    # Set only by adapters whose SOURCE EVENT names its own project (the
    # activity report). Resolution then goes through the registry by slug,
    # never through a working directory.
    project_slug: str | None = None


def _refusal(reason: str) -> None:
    """Log-channel-free refusal: adapters return None, callers count these."""
    _ = reason  # kept for debuggability at the call site


def _deny_check_path(posix_path: str, working_directory: str | None) -> str | None:
    """The project-relative form of a path for denylist checks, or None.

    The denylist is about WHERE IN THE PROJECT content lives (config/,
    secrets/, .env, scratch/), never about where the machine happens to keep
    its checkouts. Returns None (no discovery-level check) when the path is
    not under the working directory — the sidecar's post-resolution check
    against the authoritative project-relative logical path covers that case.
    """
    if working_directory:
        try:
            rel = PurePosixPath(posix_path).relative_to(
                PurePosixPath(working_directory.replace("\\", "/"))
            )
            return rel.as_posix()
        except ValueError:
            return None
    return posix_path


def from_tool_completed(envelope: dict[str, Any]) -> Candidate | None:
    """agent_tool_write: an EXPLICITLY user-facing full-file write.

    Structural shape (write-family tool, path + full inline content, supported
    text media, non-denied location) is necessary but no longer sufficient:
    the capture must ALSO carry an explicit user-facing signal — either a
    producer delivery marker on the event (data.delivered / data.user_facing /
    non-empty data.deliverables / data.attachments) or a path inside a
    project-configured producer output tree (default `_bmad_output/` and
    `_bmad-output/` planning outputs; evidence/logs/scratch/tmp excluded).
    Generic README/config/docs writes without one of those signals do NOT
    qualify — they are process, not deliverables.
    """
    data = envelope.get("data") or {}
    if not isinstance(data, dict):
        return None
    if data.get("outcome") == "error":
        return None
    tool = str(data.get("tool_name") or "")
    if _tool_family(tool) not in WRITE_TOOL_NAMES:
        return None
    arguments = tool_arguments(data)
    if not isinstance(arguments, dict):
        return None
    path_value = next(
        (arguments[k] for k in WRITE_PATH_ARGUMENTS if isinstance(arguments.get(k), str) and arguments[k]),
        None,
    )
    if path_value is None:
        return None
    content_value = next(
        (arguments[k] for k in WRITE_CONTENT_ARGUMENTS if isinstance(arguments.get(k), str)),
        None,
    )
    if content_value is None:
        return None
    posix_path = path_value.replace("\\", "/")
    name = PurePosixPath(posix_path).name or "untitled"
    media = media_type_for(name)
    if media is None:
        # Not a deliverable extension. Do not widen: a .py a tool wrote is
        # source code, not a user-facing artifact. Counted as unsupported.
        _refusal(f"unsupported extension for {name!r}")
        return None
    if media not in TEXT_INLINE_MEDIA_TYPES:
        # Binary media can never arrive honestly as an inline string; refuse
        # rather than fabricate bytes that pretend to be a PNG/PDF. Genuine
        # binary delivery happens through the delivered-file adapter.
        _refusal(f"binary media {media} cannot qualify from inline string content")
        return None
    # Content-location hygiene: project-level config/secrets/scratch paths
    # never qualify, even with a supported extension. The denylist is
    # checked against the path RELATIVE to the agent's working directory (or
    # as given when not relative), never against the machine's absolute
    # layout.
    wd = data.get("working_directory")
    wd = wd if isinstance(wd, str) else None
    deny_target = _deny_check_path(posix_path, wd)
    fragment = denied_path(deny_target) if deny_target is not None else None
    if fragment is not None:
        _refusal(f"path {name!r} matches denied fragment {fragment!r}")
        return None

    # THE DELIVERY GATE: explicit user-facing qualification. Path + inline
    # content + nice extension alone never qualified a deliverable; agents
    # write generic docs/config/readmes constantly.
    marker = explicit_delivery_marker(data)
    output_root = matches_producer_output(posix_path, wd)
    if not (marker or output_root):
        _refusal(
            f"write of {name!r} carries no explicit user-facing signal "
            "(no producer delivery marker, not in a configured producer output tree)"
        )
        return None
    gate_reason = (
        "producer delivery marker" if marker
        else "path inside project producer output tree"
    )
    payload = content_value.encode("utf-8")
    return Candidate(
        basis="agent_tool_write",
        qualification_reason=(
            f"{tool} tool call carried file_path plus full inline content "
            f"({len(payload)} bytes, {media}); {gate_reason}"
        ),
        capture=Capture(
            title=name,
            logical_path=posix_path,
            media_type=media,
            data=payload,
        ),
        working_directory=wd,
    )


_MERMAID_FENCE = re.compile(
    r"```mermaid[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE
)


def _reply_text(data: dict[str, Any]) -> str:
    reply = data.get("reply_text")
    return reply if isinstance(reply, str) else ""


def _thread_key(data: dict[str, Any]) -> str:
    """Stable thread scope: thread_id, else turn_id, else 'unscoped'."""
    for key in ("thread_id", "turn_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return "unscoped"


# Fenced-codeblock attachments a producer may reference; these deliver TEXT
# bytes that the reply itself carries, no local read needed.
_MEDIA_PATH_RE = re.compile(r"MEDIA:(/[^\\\s|)\]\"']+)")


def _delivered_media_paths(data: dict[str, Any]) -> list[str]:
    """Explicitly delivered local file paths from the producer's own markers.

    Sources (all producer-explicit, never inferred from prose):
      - data.deliverables / data.attachments entries carrying a `path` or
        `file` string (absolute, MEDIA:-style or bare);
      - `MEDIA:<absolute path>` tokens in reply_text — the delivery syntax
        Hermes final replies use.
    Deduplicated, order-preserving. Relative paths are dropped: without the
    agent cwd there is no secure anchor, and cwd is exactly what the
    read-policy roots need — the sidecar resolves them from the event's
    working_directory against operator-configured roots, never ambiently.
    """
    paths: list[str] = []

    def add(candidate: Any) -> None:
        if not isinstance(candidate, str) or not candidate:
            return
        text = candidate.strip()
        if text.startswith("MEDIA:"):
            text = text[len("MEDIA:"):]
        text = text.strip()
        if not text.startswith("/"):
            return  # absolute only: no cwd anchor, no ambient resolution
        if text not in paths:
            paths.append(text)

    for key in _DELIVERY_MARKER_LIST_KEYS:
        value = data.get(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, dict):
                    add(item.get("path"))
                    add(item.get("file"))
                else:
                    add(item)
    reply = data.get("reply_text")
    if isinstance(reply, str):
        for match in _MEDIA_PATH_RE.finditer(reply):
            add(match.group(1))
    return paths


def _delivered_file_candidate(
    envelope: dict[str, Any],
    data: dict[str, Any],
    path_text: str,
    wd: str | None,
    scope: str,
) -> "Candidate | _RefusalMarker":
    """One explicitly delivered local file, read under the capture policy.

    The read happens NOW, in the adapter, because only here do we know the
    event is producer-marked delivered. The policy chain (artifact_lib):
      - denylist fragments first (secrets/config/scratch/… never qualify);
      - path must sit under an OPERATOR-ALLOWLISTED output root resolved
        from the event's working_directory (no ambient repo read, no
        ~/.hermes cache — deliberately denied);
      - lexical symlink check on every component + O_NOFOLLOW open,
        regular file only, size bounded, digest computed from the read.

    Binary media (PNG/PDF) is allowed HERE and only here: the bytes are real,
    read from disk — never an inline string pretending to be a binary.

    Returns a Candidate, or _RefusalMarker when policy refused (the caller
    surfaces it as an observable 'skipped' disposition, not a crash).
    """
    name = PurePosixPath(path_text.replace("\\", "/")).name or "delivered"
    media = media_type_for(name)
    if media is None or media not in DELIVERED_FILE_MEDIA_TYPES:
        return _RefusalMarker(f"delivered file {name!r} has unsupported media type")
    if denied_path(path_text) is not None:
        return _RefusalMarker(f"delivered file {name!r} is in a denied content location")
    if wd is None:
        return _RefusalMarker(
            f"delivered file {name!r} has no working_directory to anchor roots"
        )
    roots = operator_capture_roots()
    try:
        blob, _sha = read_file_bounded(Path(path_text), roots)
    except (PathViolation, StoreError) as exc:
        return _RefusalMarker(f"delivered file {name!r} refused by read policy: {exc}")
    digest8 = hashlib.sha256(blob).hexdigest()[:8]
    return Candidate(
        basis="conversation_reply_delivered",
        qualification_reason=(
            f"explicitly delivered local file ({media}, {len(blob)} bytes read "
            f"under operator allowlisted roots; thread {scope})"
        ),
        capture=Capture(
            title=name,
            logical_path=f"delivered/{scope}/{digest8}/{name}",
            media_type=media,
            data=blob,
        ),
        working_directory=wd,
    )


class _RefusalMarker:
    """Adapter-level refusal for one delivered path (observable, not fatal)."""

    __slots__ = ("reason",)

    def __init__(self, reason: str):
        self.reason = reason


def operator_capture_roots() -> tuple[Path, ...]:
    """Operator-configured absolute roots a delivered file may be read from.

    ARTIFACT_SIDECAR_CAPTURE_ROOTS: absolute paths, ':'-separated, resolved
    against the event's project. Empty by default — delivered-file capture is
    strictly opt-in; the .hermes cache and arbitrary repo trees are NOT
    broadly readable, by design. Each root must be absolute; relative or
    symlinked entries are dropped (fail-closed).
    """
    raw = os.environ.get("ARTIFACT_SIDECAR_CAPTURE_ROOTS", "")
    roots: list[Path] = []
    for token in raw.split(":"):
        token = token.strip()
        if not token:
            continue
        candidate = Path(token)
        if not candidate.is_absolute():
            continue
        try:
            if candidate.is_symlink():
                continue
        except OSError:
            continue
        roots.append(candidate)
    return tuple(roots)


def from_turn_completed(envelope: dict[str, Any], *, max_chars: int = DEFAULT_MAX_REPLY_CHARS) -> list[Candidate]:
    """Inline artifacts from a completed turn reply.

    conversation_reply_mermaid: the first fenced mermaid block, scoped to the
    thread+turn and named by the block digest — identity no longer merges
    unrelated turns project-wide.

    conversation_reply_delivered: substantial reply text when the producer
    EXPLICITLY marked it user-facing/delivered (data.delivered/user_facing
    true, or data.deliverables non-empty). No marker, no artifact.
    """
    data = envelope.get("data") or {}
    if not isinstance(data, dict):
        return []
    if data.get("outcome") not in (None, "completed"):
        return []
    reply = _reply_text(data)
    if len(reply) > max_chars:
        reply = reply[:max_chars]
    candidates: list[Candidate] = []
    refusals: list[str] = []
    wd = data.get("working_directory")
    wd = wd if isinstance(wd, str) else None
    scope = _thread_key(data)

    # conversation_reply_mermaid stays STRUCTURAL (an inline diagram in the
    # reply is itself the deliverable, bytes already in the event); the
    # explicit marker governs reply-text and delivered-LOCAL-FILE capture —
    # a diagram request never authorizes reading files, and full reply text
    # is captured only when the producer marked it delivered.
    explicit = explicit_delivery_marker(data)

    for index, match in enumerate(_MERMAID_FENCE.finditer(reply), start=1):
        body = match.group(1)
        if not body.strip():
            continue
        payload = body.rstrip("\r\n").encode("utf-8")
        digest8 = hashlib.sha256(payload).hexdigest()[:8]
        title = f"diagram-{digest8}.mmd"
        # Logical identity: thread + block digest. Same diagram re-discussed
        # in the SAME thread chains revisions on one artifact id; a different
        # thread is a different artifact. A stable per-thread label for the
        # FIRST diagram keeps the common case readable; digest-named later
        # blocks avoid collisions between distinct diagrams in one thread.
        logical = f"inline/mermaid/{scope}/{digest8}"
        candidates.append(
            Candidate(
                basis="conversation_reply_mermaid",
                qualification_reason=(
                    f"fenced mermaid block {index} in conversation.turn.completed "
                    f"reply_text (thread {scope}, {len(payload)} bytes)"
                ),
                capture=Capture(
                    title=title,
                    logical_path=logical,
                    media_type="text/vnd.mermaid",
                    data=payload,
                ),
                working_directory=wd,
            )
        )

    if explicit:
        delivered = _delivered_reply_candidate(data, reply, wd, scope)
        if isinstance(delivered, Candidate):
            candidates.append(delivered)
        # Explicitly DELIVERED LOCAL FILES: the only path to genuine binary
        # bytes (PNG/PDF), read under the operator-allowlisted roots policy.
        for path_text in _delivered_media_paths(data):
            result = _delivered_file_candidate(envelope, data, path_text, wd, scope)
            if isinstance(result, Candidate):
                candidates.append(result)
            else:
                refusals.append(result.reason)
        if refusals:
            _refusal("; ".join(refusals))
    return candidates


_DELIVERED_MIN_SUBSTANTIVE_CHARS = 80


def _delivered_reply_candidate(
    data: dict[str, Any], reply: str, wd: str | None, scope: str
) -> Candidate | None:
    """Explicit producer-marked delivered reply text, as markdown."""
    explicit = bool(data.get("delivered")) or bool(data.get("user_facing"))
    deliverables = data.get("deliverables")
    explicit = explicit or (
        isinstance(deliverables, (list, tuple)) and len(deliverables) > 0
    )
    if not explicit:
        return None
    stripped = reply.strip()
    if len(stripped) < _DELIVERED_MIN_SUBSTANTIVE_CHARS:
        return None  # a marker on an empty/trivial reply captures nothing
    marker = "user_facing=true" if data.get("user_facing") else (
        "delivered=true" if data.get("delivered") else "deliverables present"
    )
    digest8 = hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:8]
    return Candidate(
        basis="conversation_reply_delivered",
        qualification_reason=(
            f"reply explicitly marked {marker} by producer "
            f"(thread {scope}, {len(stripped)} chars)"
        ),
        capture=Capture(
            title=f"reply-{digest8}.md",
            logical_path=f"inline/replies/{scope}/{digest8}",
            media_type="text/markdown",
            data=stripped.encode("utf-8"),
        ),
        working_directory=wd,
    )


ACTIVITY_RECORDED_TYPE = "bloodbank.project.activity.recorded"
_ACTIVITY_AUDIENCES = ("internal", "external")
_SLUG_OK = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _compact_ts(value: str) -> str:
    """20261016T073001Z from an RFC3339 timestamp; digits/letters only."""
    return re.sub(r"[^0-9A-Za-z]", "", value.replace("-", "").replace(":", "").split(".")[0]) or "unknown"


def _compact_minutes(value: str) -> str:
    """20261016T0730Z (minute resolution, for the human-visible filename)."""
    compact = _compact_ts(value)
    return compact[:13] + "Z" if len(compact) >= 13 else compact


def from_activity_recorded(envelope: dict[str, Any]) -> list[Candidate]:
    """project_activity_report: one rendered activity report as an artifact.

    The report event names its own project (`data.project.slug`) so there is
    no working-directory attribution; the sidecar still requires the slug to
    be enrolled in the pjangler registry.

    Identity (stable per project + audience + window): the logical path is
    `inline/activity/<audience>/<window.start>`, so the same window re-emitted
    with different bytes chains a REVISION on one artifact id, while a
    different window (different `window.start`) is a different artifact. With
    `window.basis == previous_report` the start is the previous report's end,
    which a re-render of the same window shares; a `cap_24h` window slides
    with the run clock, so a re-run later is honestly a different window.

    Form: the report markdown is the canonical bytes (the artifact an agent
    or human reads) and the report's self-contained HTML is the `render` twin
    (the durable artifact the schema names). A report that somehow lacks
    markdown falls back to the HTML as canonical, then to the raw text.
    `generator.dry_run` is recorded in the qualification reason but does not
    disqualify: dry_run means "nothing was pushed to the portal", and the
    vault copy is exactly what such a run is for.
    """
    data = envelope.get("data")
    if not isinstance(data, dict):
        return []
    project = data.get("project")
    slug = project.get("slug") if isinstance(project, dict) else None
    if not isinstance(slug, str) or not _SLUG_OK.match(slug):
        return []
    audience = data.get("audience")
    if audience not in _ACTIVITY_AUDIENCES:
        return []
    window = data.get("window")
    start = window.get("start") if isinstance(window, dict) else None
    end = window.get("end") if isinstance(window, dict) else None
    if not isinstance(start, str) or not start or not isinstance(end, str) or not end:
        return []
    report = data.get("report")
    if not isinstance(report, dict):
        return []

    def text(key: str) -> str | None:
        value = report.get(key)
        return value if isinstance(value, str) and value.strip() else None

    markdown, html, raw = text("markdown"), text("html"), text("raw")
    stem = f"activity-{audience}-{_compact_minutes(end)}"
    if markdown is not None:
        canonical = Capture(
            title=f"{stem}.md",
            logical_path="",  # set below with the shared logical path
            media_type="text/markdown",
            data=markdown.encode("utf-8"),
        )
        render_html = html
        form = "markdown canonical" + (" + html render twin" if html else "")
    elif html is not None:
        canonical = Capture(title=f"{stem}.html", logical_path="", media_type="text/html",
                            data=html.encode("utf-8"))
        render_html = None
        form = "html canonical (report carried no markdown)"
    elif raw is not None:
        canonical = Capture(title=f"{stem}.txt", logical_path="", media_type="text/plain",
                            data=raw.encode("utf-8"))
        render_html = None
        form = "raw text canonical (report carried no markdown or html)"
    else:
        return []

    logical = f"inline/activity/{audience}/{_compact_ts(start)}"
    render = None
    if render_html is not None:
        render = Capture(
            title=f"{stem}.render.html",
            logical_path=logical,
            media_type="text/html",
            data=render_html.encode("utf-8"),
        )
    capture = Capture(
        title=canonical.title,
        logical_path=logical,
        media_type=canonical.media_type,
        data=canonical.data,
        render=render,
    )
    generator = data.get("generator")
    gen = generator if isinstance(generator, dict) else {}
    dry = " dry_run=true (not portal-published)" if gen.get("dry_run") is True else ""
    basis = window.get("basis") if isinstance(window.get("basis"), str) else "unknown"
    return [
        Candidate(
            basis="project_activity_report",
            qualification_reason=(
                f"bloodbank.project.activity.recorded report for {slug}: audience {audience}, "
                f"window {start}..{end} ({basis}); {form}; generator "
                f"{gen.get('skill') or 'unknown'}{dry}"
            )[:500],
            capture=capture,
            working_directory=None,
            project_slug=slug,
        )
    ]


def hold_record_from(
    envelope: dict[str, Any],
    candidate: Candidate,
    *,
    reason: str,
    sha256: str,
    ref: str,
) -> dict[str, Any]:
    """Hold record for an unattributable candidate, keeping provenance."""
    from artifact_lib import hold_record

    return hold_record(
        reason=reason,
        source_event_id=str(envelope.get("id")),
        source_event_type=str(envelope.get("type")),
        source_event_time=str(envelope.get("time")),
        working_directory=candidate.working_directory,
        title=candidate.capture.title,
        sha256=sha256,
        bytes_len=len(candidate.capture.data),
        media_type=candidate.capture.media_type,
        ref=ref,
        logical_path=candidate.capture.logical_path,
    )


__all__ = [
    "DEFAULT_PRODUCER_OUTPUT_GLOBS",
    "PRODUCER_OUTPUT_EXCLUDED_PARTS",
    "PRODUCER_OUTPUT_EXTENSIONS",
    "Candidate",
    "Capture",
    "WRITE_TOOL_NAMES",
    "explicit_delivery_marker",
    "from_activity_recorded",
    "from_tool_completed",
    "from_turn_completed",
    "hold_record_from",
    "matches_producer_output",
    "operator_capture_roots",
    "tool_arguments",
    "write_like_but_unsupported",
]
