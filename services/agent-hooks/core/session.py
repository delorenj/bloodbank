"""Per-CLI session state for hook publishers.

Persists the native session id (mapped to a UUID ``correlationid``), the previous event
id (used as ``causationid`` on the next event so the chain is linked),
a turn counter, and per-tool usage counters. Each CLI chooses its own
on-disk path.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _git(*args: str, cwd: str | None = None) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def git_branch(cwd: str | None = None) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd) or "unknown"


def git_remote(cwd: str | None = None) -> str:
    return _git("remote", "get-url", "origin", cwd=cwd)


def git_status_word(cwd: str | None = None) -> str:
    return "modified" if _git("status", "--porcelain", cwd=cwd) else "clean"


def git_files_modified(cwd: str | None = None) -> list[str]:
    out = _git("diff", "--name-only", cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


def git_commits_since(since_iso: str, cwd: str | None = None) -> list[str]:
    out = _git("log", f"--since={since_iso}", "--format=%H", cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


class SessionState:
    """File-backed per-CLI session state.

    Keys:
        session_id      — native identity, stable for the life of the CLI session
        last_event_id   — id of the previously published event, used as causation_id
        started_at      — ISO timestamp of session start
        working_directory, git_branch
        turn_number     — bumped on each tool-use event
        conversation_turn_number, current_turn_id — prompt/response identity
        tools_used      — name → count
    """

    def __init__(self, path: Path, *, working_directory: str | None = None,
                 native_id: str | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cwd = working_directory or os.getcwd()
        self._native_id = native_id
        self._data: dict[str, Any] = self._load()

    def _fresh(self) -> dict[str, Any]:
        sid = self._native_id or str(uuid.uuid4())
        return {
            "session_id": sid,
            "started_at": _now_iso(),
            "working_directory": self._cwd,
            "git_branch": git_branch(self._cwd),
            "turn_number": 0,
            "conversation_turn_number": 0,
            "tools_used": {},
            # The adapter uses the UUID correlation id before the first event.
            "last_event_id": "",
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._fresh()
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return self._fresh()

    def _save(self) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent,
                                             prefix=".session-", delete=False) as out:
                temporary = out.name
                json.dump(self._data, out, indent=2)
            os.replace(temporary, self.path)
        except OSError:
            pass
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    @property
    def session_id(self) -> str:
        return self._data["session_id"]

    @property
    def last_event_id(self) -> str:
        return self._data["last_event_id"]

    @property
    def working_directory(self) -> str:
        return self._data.get("working_directory", self._cwd)

    @property
    def git_branch(self) -> str:
        return self._data.get("git_branch", "unknown")

    @property
    def turn_number(self) -> int:
        return int(self._data.get("turn_number", 0))

    @property
    def conversation_turn_number(self) -> int:
        return int(self._data.get("conversation_turn_number", 0))

    @property
    def current_turn_id(self) -> str:
        return self._data.get("current_turn_id") or f"{self.session_id}:turn:{max(self.conversation_turn_number, 1)}"

    def begin_turn(self, native_turn_id: str | None = None) -> str:
        """Persist one prompt/response identity independently of tool counts."""
        self._data["conversation_turn_number"] = self.conversation_turn_number + 1
        self._data["current_turn_id"] = native_turn_id or f"{self.session_id}:turn:{self.conversation_turn_number}"
        self._save()
        return self.current_turn_id

    @property
    def tools_used(self) -> dict[str, int]:
        return dict(self._data.get("tools_used", {}))

    @property
    def started_at(self) -> str:
        return self._data.get("started_at", _now_iso())

    def reset(self, native_id: str | None = None) -> None:
        """Initialize state while preserving an authoritative native id."""
        self._native_id = native_id or self._native_id
        self._data = self._fresh()
        self._save()

    def record_event(self, event_id: str) -> None:
        """Set last_event_id so the next event's causation_id points here."""
        self._data["last_event_id"] = event_id
        self._save()

    def bump_tool(self, tool_name: str) -> None:
        used = self._data.setdefault("tools_used", {})
        used[tool_name] = int(used.get(tool_name, 0)) + 1
        self._data["turn_number"] = int(self._data.get("turn_number", 0)) + 1
        self._save()

    def archive(self, archive_dir: Path) -> None:
        """Move the session file into archive_dir on session end. Best effort."""
        if not self.path.exists():
            return
        archive_dir.mkdir(parents=True, exist_ok=True)
        try:
            safe_id = hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()
            self.path.rename(archive_dir / f"{safe_id}.json")
        except OSError:
            pass
