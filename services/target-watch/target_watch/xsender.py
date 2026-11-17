"""The x-sender index: where each inbound type keeps its sender, read from the schemas.

Every inbound schema carries a top-level "x-sender" annotation (docs/event-naming.md
section 10.1). This module turns the schema tree into {type: XSender} and
reloads it when a schema file changes, so a provider that gains an x-sender
block is matched by every watch without a code change or a restart.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .handles import HANDLE_KINDS

REQUIRED_KEYS = ("handle_paths", "name_paths", "thread_path", "text_paths", "time_path", "inbound_when", "handle_kinds")


class XSenderError(ValueError):
    pass


@dataclass(frozen=True)
class XSender:
    type: str
    handle_paths: tuple[str, ...]
    name_paths: tuple[str, ...]
    thread_path: str | None
    text_paths: tuple[str, ...]
    time_path: str | None
    inbound_path: str | None
    inbound_equals: tuple[str, ...]
    handle_kinds: tuple[str, ...]
    # Optional extension: a subject line (email-like providers). Not required by the contract.
    subject_path: str | None = None
    schema_path: str | None = None


def _path_list(block: dict, key: str) -> tuple[str, ...]:
    value = block.get(key)
    if not isinstance(value, list) or not all(isinstance(p, str) and p.startswith("data.") for p in value):
        raise XSenderError(f"x-sender.{key} must be a list of 'data.*' paths")
    return tuple(value)


def _path(block: dict, key: str, *, nullable: bool) -> str | None:
    value = block.get(key)
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.startswith("data."):
        raise XSenderError(f"x-sender.{key} must be a 'data.*' path")
    return value


def parse_block(ce_type: str, block: object, schema_path: str | None = None) -> XSender:
    if not isinstance(block, dict):
        raise XSenderError("x-sender must be an object")
    missing = [k for k in REQUIRED_KEYS if k not in block]
    if missing:
        raise XSenderError(f"x-sender is missing {missing}")
    handle_paths = _path_list(block, "handle_paths")
    if not handle_paths:
        raise XSenderError("x-sender.handle_paths must name at least one path")
    kinds = block.get("handle_kinds")
    if not isinstance(kinds, list) or not kinds or any(k not in HANDLE_KINDS for k in kinds):
        raise XSenderError(f"x-sender.handle_kinds must be a non-empty subset of {list(HANDLE_KINDS)}")
    inbound = block.get("inbound_when")
    inbound_path, inbound_equals = None, ()
    if inbound is not None:
        if not isinstance(inbound, dict):
            raise XSenderError("x-sender.inbound_when must be null or {path, equals}")
        inbound_path = _path(inbound, "path", nullable=False)
        equals = inbound.get("equals")
        if not isinstance(equals, list) or not equals or not all(isinstance(v, str) for v in equals):
            raise XSenderError("x-sender.inbound_when.equals must be a non-empty list of strings")
        inbound_equals = tuple(equals)
    subject_path = block.get("subject_path")
    if subject_path is not None and (not isinstance(subject_path, str) or not subject_path.startswith("data.")):
        raise XSenderError("x-sender.subject_path must be a 'data.*' path")
    return XSender(
        type=ce_type,
        handle_paths=handle_paths,
        name_paths=_path_list(block, "name_paths"),
        thread_path=_path(block, "thread_path", nullable=True),
        text_paths=_path_list(block, "text_paths"),
        time_path=_path(block, "time_path", nullable=True),
        inbound_path=inbound_path,
        inbound_equals=inbound_equals,
        handle_kinds=tuple(kinds),
        subject_path=subject_path,
        schema_path=schema_path,
    )


def scan(root: Path) -> tuple[dict[str, XSender], list[str]]:
    """Every schema under <root>/bloodbank/<domain>/ that carries x-sender."""
    index: dict[str, XSender] = {}
    errors: list[str] = []
    for path in sorted(Path(root).glob("bloodbank/*/*.json")):
        try:
            schema = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: unreadable ({type(exc).__name__})")
            continue
        if not isinstance(schema, dict) or "x-sender" not in schema:
            continue
        ce_type = (((schema.get("properties") or {}).get("type") or {}).get("const"))
        if not isinstance(ce_type, str):
            errors.append(f"{path}: x-sender on a schema without properties.type.const")
            continue
        try:
            index[ce_type] = parse_block(ce_type, schema["x-sender"], str(path))
        except XSenderError as exc:
            errors.append(f"{path}: {exc}")
    return index, errors


class XSenderIndex:
    """Cached {type: XSender}; rescans when a schema file appears, vanishes or changes."""

    def __init__(self, root: Path, recheck_s: float = 30.0, log=None):
        self.root = Path(root)
        self.recheck_s = recheck_s
        self.log = log
        self._signature: tuple | None = None
        self._checked = 0.0
        self.index: dict[str, XSender] = {}
        self.errors: list[str] = []
        self.reload()

    def _current_signature(self) -> tuple:
        out = []
        for path in sorted(self.root.glob("bloodbank/*/*.json")):
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append((path.name, path.parent.name, stat.st_mtime_ns, stat.st_size))
        return tuple(out)

    def reload(self) -> None:
        self._signature = self._current_signature()
        self._checked = time.monotonic()
        self.index, self.errors = scan(self.root)
        if self.log:
            self.log("x_sender_loaded", types=len(self.index), errors=len(self.errors))
            for error in self.errors:
                self.log("x_sender_error", error=error)

    def _maybe_reload(self) -> None:
        if time.monotonic() - self._checked >= self.recheck_s:
            self._checked = time.monotonic()
            if self._current_signature() != self._signature:
                self.reload()

    def get(self, ce_type: str) -> XSender | None:
        self._maybe_reload()
        return self.index.get(ce_type)

    def all(self) -> dict[str, XSender]:
        self._maybe_reload()
        return dict(self.index)
