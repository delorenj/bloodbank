"""Watched targets: TOML files discovered by glob, reloaded when they change.

    [target]
    id = "jane-doe"
    display_name = "Jane Doe"

    [target.handles]
    email = ["jane@example.com"]
    e164 = ["+12155550100"]
    label = ["Jane Doe"]
    platform = ["slack:U0123ABCD", "telegram:123456789"]

    [sink]
    kind = "plane"               # or "notify_only"
    repo = "~/code/some-project" # px binds the board from this repo's .project.json
    state = "Todo"
    labels = ["submitted-by-client"]
    priority = "medium"
    coalesce_hours = 24
    notify = ["telegram:<chat id>"]
"""
from __future__ import annotations

import glob
import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .handles import normalise_target_handle
from .paths import target_globs

TARGET_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
CHANNEL = re.compile(r"^[a-z][a-z0-9_]*:\S+$")
PRIORITIES = {"urgent", "high", "medium", "low", "none"}
SINK_KINDS = {"plane", "notify_only"}
HANDLE_KEYS = ("email", "e164", "label", "platform")


class TargetError(ValueError):
    pass


@dataclass(frozen=True)
class Sink:
    kind: str
    repo: Path | None = None
    state: str | None = None
    labels: tuple[str, ...] = ()
    priority: str | None = None
    coalesce_hours: float = 24.0
    notify: tuple[str, ...] = ()


@dataclass(frozen=True)
class Target:
    id: str
    display_name: str
    sink: Sink
    path: Path
    email: frozenset[str] = field(default_factory=frozenset)
    e164: frozenset[str] = field(default_factory=frozenset)
    label: frozenset[str] = field(default_factory=frozenset)
    platform: frozenset[str] = field(default_factory=frozenset)

    def handles(self, kind: str) -> frozenset[str]:
        return {
            "email": self.email,
            "e164": self.e164,
            "label": self.label,
            "platform_user": self.platform,
            "platform": self.platform,
        }[kind]

    def handle_count(self) -> int:
        return len(self.email) + len(self.e164) + len(self.label) + len(self.platform)


def _strings(value: object, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TargetError(f"{where} must be a list of strings")
    return value


def parse_target(document: dict, path: Path) -> Target:
    target = document.get("target")
    if not isinstance(target, dict):
        raise TargetError("missing [target] table")
    target_id = target.get("id")
    if not isinstance(target_id, str) or not TARGET_ID.match(target_id):
        raise TargetError("target.id must be a lowercase slug ([a-z0-9][a-z0-9_-]*)")
    display = target.get("display_name")
    if not isinstance(display, str) or not display.strip():
        raise TargetError("target.display_name is required")
    raw_handles = target.get("handles") or {}
    if not isinstance(raw_handles, dict):
        raise TargetError("[target.handles] must be a table")
    unknown = set(raw_handles) - set(HANDLE_KEYS)
    if unknown:
        raise TargetError(f"unknown handle kinds {sorted(unknown)}; use {list(HANDLE_KEYS)}")
    handles: dict[str, set[str]] = {k: set() for k in HANDLE_KEYS}
    for kind in HANDLE_KEYS:
        for raw in _strings(raw_handles.get(kind), f"target.handles.{kind}"):
            normalised = normalise_target_handle(kind, raw)
            if normalised is None:
                # Never echo the handle itself: target files hold personal data.
                raise TargetError(f"target.handles.{kind} has an entry that is not a valid {kind} handle")
            handles[kind].add(normalised)
    if not any(handles.values()):
        raise TargetError("a target needs at least one handle")

    sink_doc = document.get("sink")
    if not isinstance(sink_doc, dict):
        raise TargetError("missing [sink] table")
    kind = sink_doc.get("kind")
    if kind not in SINK_KINDS:
        raise TargetError(f"sink.kind must be one of {sorted(SINK_KINDS)}")
    notify = tuple(_strings(sink_doc.get("notify"), "sink.notify"))
    for channel in notify:
        if not CHANNEL.match(channel):
            raise TargetError("sink.notify entries look like 'telegram:<chat id>'")
    labels = tuple(_strings(sink_doc.get("labels"), "sink.labels"))
    priority = sink_doc.get("priority")
    if priority is not None and priority not in PRIORITIES:
        raise TargetError(f"sink.priority must be one of {sorted(PRIORITIES)}")
    coalesce = sink_doc.get("coalesce_hours", 24)
    if isinstance(coalesce, bool) or not isinstance(coalesce, (int, float)) or coalesce < 0:
        raise TargetError("sink.coalesce_hours must be a number >= 0")
    state = sink_doc.get("state")
    if state is not None and (not isinstance(state, str) or not state.strip()):
        raise TargetError("sink.state must be a state name")
    repo = None
    if kind == "plane":
        raw_repo = sink_doc.get("repo")
        if not isinstance(raw_repo, str) or not raw_repo.strip():
            raise TargetError("a plane sink needs sink.repo (the repo whose .project.json binds the board)")
        repo = Path(os.path.expanduser(raw_repo))
    return Target(
        id=target_id,
        display_name=display.strip(),
        path=path,
        email=frozenset(handles["email"]),
        e164=frozenset(handles["e164"]),
        label=frozenset(handles["label"]),
        platform=frozenset(handles["platform"]),
        sink=Sink(
            kind=kind,
            repo=repo,
            state=state,
            labels=labels,
            priority=priority,
            coalesce_hours=float(coalesce),
            notify=notify,
        ),
    )


def discover(globs: list[str]) -> list[Path]:
    found: dict[str, Path] = {}
    for pattern in globs:
        for name in glob.glob(os.path.expanduser(pattern.strip())):
            path = Path(name)
            if path.is_file():
                found[str(path.resolve())] = path.resolve()
    return [found[k] for k in sorted(found)]


def load_targets(globs: list[str] | None = None) -> tuple[list[Target], list[str]]:
    """(targets, errors). A bad file is reported and skipped, never fatal."""
    targets: list[Target] = []
    errors: list[str] = []
    seen: dict[str, Path] = {}
    for path in discover(globs if globs is not None else target_globs()):
        try:
            with path.open("rb") as handle:
                target = parse_target(tomllib.load(handle), path)
        except (OSError, tomllib.TOMLDecodeError, TargetError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        if target.id in seen:
            errors.append(f"{path}: duplicate target id {target.id!r} (first in {seen[target.id]})")
            continue
        seen[target.id] = path
        targets.append(target)
    return targets, errors


class TargetRegistry:
    """Targets reloaded when the set of files or any mtime changes (checked every recheck_s)."""

    def __init__(self, globs: list[str] | None = None, recheck_s: float = 30.0, log=None):
        self.globs = globs if globs is not None else target_globs()
        self.recheck_s = recheck_s
        self.log = log
        self._signature: tuple | None = None
        self._checked = 0.0
        self.targets: list[Target] = []
        self.errors: list[str] = []
        self.reload()

    def _current_signature(self) -> tuple:
        out = []
        for path in discover(self.globs):
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(out)

    def reload(self) -> None:
        self._signature = self._current_signature()
        self._checked = time.monotonic()
        self.targets, self.errors = load_targets(self.globs)
        if self.log:
            self.log("targets_loaded", targets=[t.id for t in self.targets], errors=len(self.errors))
            for error in self.errors:
                self.log("target_error", error=error)

    def current(self) -> list[Target]:
        if time.monotonic() - self._checked >= self.recheck_s:
            self._checked = time.monotonic()
            if self._current_signature() != self._signature:
                self.reload()
        return self.targets

    def get(self, target_id: str) -> Target | None:
        return next((t for t in self.current() if t.id == target_id), None)
