#!/usr/bin/env python3
"""Claude Code → Bloodbank hook publisher (v1 contract).

Reads a Claude Code hook payload from stdin, builds a CloudEvents 1.0
envelope per bloodbank/docs/event-naming.md, and publishes to NATS at
``bloodbank.evt.v1.<domain>.<entity>.<action>``. Fire-and-forget; never
fails the agent.

Usage (from .claude/settings.json):
    python3 .../agent-hooks/claude/publish.py <event-type> [end-reason]

Event types (CLI argument → v1 CloudEvents type). The map is sourced from
claude/event_map.generated.json (projected from hooks.master.json by sync.py)
and merged over the embedded fallback below — do NOT hand-edit it; edit
hooks.master.json then run `mise run hooks:sync`:
    session-start    → bloodbank.v1.agent.session.started
    session-end      → bloodbank.v1.agent.session.ended
    prompt-submitted → bloodbank.v1.conversation.turn.started
    tool-request     → bloodbank.v1.agent.tool.requested
    tool-action      → bloodbank.v1.agent.tool.completed
    subagent-stopped → bloodbank.v1.agent.invocation.completed

Session state lives at ~/.claude/bloodbank-session.json (single global
session across every Claude Code invocation, regardless of cwd) and is
archived to ~/.claude/bloodbank-sessions/<session_id>.json on session-end.
Per-event git/working-directory context is captured live from cwd.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Make ``core`` importable when invoked as a standalone script.
SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from core.envelope import build_envelope  # noqa: E402
from core.event_map import resolve_map  # noqa: E402
from core.nats_publish import publish as nats_publish  # noqa: E402
from core.session import (  # noqa: E402
    SessionState,
    _now_iso,
    git_branch,
    git_commits_since,
    git_files_modified,
    git_remote,
    git_status_word,
)

# User-scoped state — single global session for all Claude Code invocations.
CLAUDE_STATE_DIR = Path.home() / ".claude"
SESSION_FILE = CLAUDE_STATE_DIR / "bloodbank-session.json"
SESSIONS_DIR = CLAUDE_STATE_DIR / "bloodbank-sessions"
ERROR_LOG = SESSIONS_DIR / "publish-errors.log"

CLAUDE_SOURCE = "urn:33god:agent:claude-code"
CLAUDE_PRODUCER = "claude-code"
CLAUDE_SERVICE = "claude-code"
CLAUDE_ACTOR: dict[str, Any] = {
    "type": "agent_cli",
    "agent_id": "bloodbank.agent.claude",
    "cli": "claude",
    "provider": "anthropic",
    "model": None,
}

# Embedded fallback. The active map is sourced from
# claude/event_map.generated.json (projected from hooks.master.json by sync.py)
# and merged over this default; `session-stop` is kept as an alias for the
# Stop hook's historical arg. Keep in sync via `mise run hooks:sync`.
_DEFAULT_MAP: dict[str, tuple[str, str]] = {
    "session-start": ("bloodbank.v1.agent.session.started", "session"),
    "session-end": ("bloodbank.v1.agent.session.ended", "session"),
    "session-stop": ("bloodbank.v1.agent.session.ended", "session"),
    "prompt-submitted": ("bloodbank.v1.conversation.turn.started", "thread"),
    "tool-request": ("bloodbank.v1.agent.tool.requested", "invocation"),
    "tool-action": ("bloodbank.v1.agent.tool.completed", "invocation"),
    "subagent-stopped": ("bloodbank.v1.agent.invocation.completed", "invocation"),
}

# CLI hook arg → (v1 CloudEvents type, ordering bucket prefix)
EVENT_MAP: dict[str, tuple[str, str]] = resolve_map(
    Path(__file__).resolve().parent, _DEFAULT_MAP
)


def _log_error(msg: str) -> None:
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        if ERROR_LOG.exists() and ERROR_LOG.stat().st_size > 1_048_576:
            try:
                ERROR_LOG.rename(ERROR_LOG.with_suffix(ERROR_LOG.suffix + ".1"))
            except OSError:
                pass
        with ERROR_LOG.open("a") as f:
            f.write(f"{_now_iso()} [{os.getpid()}] {msg}\n")
    except OSError:
        pass
    if os.environ.get("BLOODBANK_DEBUG") == "true":
        print(f"[bloodbank-claude-hook] {msg}", file=sys.stderr)


def _read_stdin() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {"raw": out}
    except json.JSONDecodeError:
        return {"raw": raw}


def _tool_call_id(session: SessionState, tool_name: str, turn_number: int) -> str:
    """Deterministic tool_call_id shared by request/complete pairs of the same tool use."""
    seed = f"{session.session_id}:{turn_number}:{tool_name}"
    return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:32]


def _build_event(
    session: SessionState,
    ce_type: str,
    bucket_prefix: str,
    data: dict[str, Any],
    *,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build a v1 event envelope rooted at the session's correlation chain."""
    # Ordering bucket maps the session_id to whichever entity the event
    # belongs to. session_id stands in for thread_id / invocation_id /
    # session_id since Claude Code's hook surface doesn't currently
    # distinguish them.
    ordering_key = f"{bucket_prefix}:{session.session_id}"
    return build_envelope(
        ce_type=ce_type,
        kind="event",
        source=CLAUDE_SOURCE,
        producer=CLAUDE_PRODUCER,
        service=CLAUDE_SERVICE,
        actor=CLAUDE_ACTOR,
        data=data,
        correlation_id=session.session_id,
        causation_id=session.last_event_id,
        ordering_key=ordering_key,
        event_id=event_id,
    )


def _handle_session_start(session: SessionState, ce_type: str, bucket: str) -> dict:
    session.reset()
    cwd = os.getcwd()
    data = {
        "session_id": session.session_id,
        "working_directory": cwd,
        "git_branch": git_branch(cwd),
        "git_remote": git_remote(cwd),
        "started_at": session.started_at,
    }
    return _build_event(
        session,
        ce_type,
        bucket,
        data,
        # First event id == session id so the chain self-roots cleanly.
        event_id=session.session_id,
    )


def _handle_prompt_submitted(
    session: SessionState, ce_type: str, bucket: str, payload: dict
) -> dict:
    prompt_text = str(payload.get("prompt", ""))
    cwd = os.getcwd()
    turn_id = f"{session.session_id}:{session.turn_number + 1}"
    data = {
        "thread_id": session.session_id,
        "turn_id": turn_id,
        "prompt_text": prompt_text,
        "prompt_length": len(prompt_text.encode("utf-8")),
        "working_directory": cwd,
        "git_branch": git_branch(cwd),
    }
    return _build_event(session, ce_type, bucket, data)


def _handle_tool_requested(
    session: SessionState, ce_type: str, bucket: str, payload: dict
) -> dict:
    tool_name = str(payload.get("tool_name", "unknown"))
    tool_input = payload.get("tool_input") or {}
    cwd = os.getcwd()
    turn_number = session.turn_number + 1
    data = {
        "invocation_id": session.session_id,
        "tool_call_id": _tool_call_id(session, tool_name, turn_number),
        "tool_name": tool_name,
        "arguments": tool_input,
        "working_directory": cwd,
        "git_branch": git_branch(cwd),
        "turn_number": turn_number,
    }
    return _build_event(session, ce_type, bucket, data)


def _tool_outcome(payload: dict) -> str:
    """Infer success/error for a Claude PostToolUse payload (fires post-execution)."""
    if isinstance(payload, dict):
        if payload.get("is_error"):
            return "error"
        resp = payload.get("tool_response") or payload.get("tool_result")
        if isinstance(resp, dict) and (resp.get("error") or resp.get("is_error")):
            return "error"
    return "success"


def _handle_tool_completed(
    session: SessionState, ce_type: str, bucket: str, payload: dict
) -> dict:
    tool_name = str(payload.get("tool_name", "unknown"))
    tool_input = payload.get("tool_input") or {}
    cwd = os.getcwd()
    turn_number = session.turn_number + 1
    data = {
        "invocation_id": session.session_id,
        "tool_call_id": _tool_call_id(session, tool_name, turn_number),
        "tool_name": tool_name,
        "arguments": tool_input,
        "outcome": _tool_outcome(payload),
        "working_directory": cwd,
        "git_branch": git_branch(cwd),
        "git_status": git_status_word(cwd),
        "turn_number": turn_number,
    }
    return _build_event(session, ce_type, bucket, data)


def _handle_subagent_completed(session: SessionState, ce_type: str, bucket: str) -> dict:
    data = {
        "invocation_id": session.session_id,
        "stop_reason": "completed",
        "working_directory": os.getcwd(),
    }
    return _build_event(session, ce_type, bucket, data)


def _handle_session_end(
    session: SessionState, ce_type: str, bucket: str, end_reason: str
) -> dict | None:
    if not session.path.exists():
        return None
    try:
        started = datetime.fromisoformat(session.started_at.replace("Z", "+00:00"))
        duration = int((datetime.now(started.tzinfo) - started).total_seconds())
    except (ValueError, TypeError):
        duration = 0
    cwd = os.getcwd()
    data = {
        "session_id": session.session_id,
        "end_reason": end_reason,
        "duration_seconds": duration,
        "total_turns": session.turn_number,
        "tools_used": session.tools_used,
        "files_modified": git_files_modified(cwd),
        "git_commits": git_commits_since(session.started_at, cwd),
        "final_status": "success",
        "working_directory": cwd,
        "git_branch": git_branch(cwd),
    }
    return _build_event(session, ce_type, bucket, data)


def _publish(envelope: dict, session: SessionState) -> None:
    """Publish envelope to NATS at envelope['subject'] and update session causation chain."""
    if os.environ.get("BLOODBANK_ENABLED", "true") != "true":
        return
    subject = envelope["subject"]
    body = json.dumps(envelope).encode("utf-8")
    try:
        nats_publish(subject, body, client_name="agent-hooks-claude")
    except (OSError, RuntimeError) as exc:
        _log_error(f"publish failed subject={subject} err={exc}")
        if os.environ.get("BLOODBANK_HOOK_STRICT") == "1":
            raise
        return
    session.record_event(envelope["id"])
    if os.environ.get("BLOODBANK_HOOK_VERBOSE"):
        print(f"[bloodbank-claude-hook] published {subject}", file=sys.stderr)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    event_type = argv[1].strip()
    mapping = EVENT_MAP.get(event_type)
    if mapping is None:
        _log_error(f"unknown event-type: {event_type}")
        return 0
    ce_type, bucket = mapping

    session = SessionState(path=SESSION_FILE)
    payload = _read_stdin()

    # Dispatch by resolved ce_type so that argument aliases (e.g. the Stop
    # hook's session-stop / session-end) route to the same handler.
    try:
        if ce_type == "bloodbank.v1.agent.session.started":
            envelope: dict | None = _handle_session_start(session, ce_type, bucket)
        elif ce_type == "bloodbank.v1.agent.session.ended":
            end_reason = argv[2] if len(argv) > 2 else "user_stop"
            envelope = _handle_session_end(session, ce_type, bucket, end_reason)
        elif ce_type == "bloodbank.v1.conversation.turn.started":
            envelope = _handle_prompt_submitted(session, ce_type, bucket, payload)
        elif ce_type == "bloodbank.v1.agent.tool.requested":
            envelope = _handle_tool_requested(session, ce_type, bucket, payload)
        elif ce_type == "bloodbank.v1.agent.tool.completed":
            envelope = _handle_tool_completed(session, ce_type, bucket, payload)
            session.bump_tool(str(payload.get("tool_name", "unknown")))
        elif ce_type == "bloodbank.v1.agent.invocation.completed":
            envelope = _handle_subagent_completed(session, ce_type, bucket)
        else:
            _log_error(f"unhandled ce_type for event_type={event_type}: {ce_type}")
            return 0
    except Exception as exc:  # never break the agent
        _log_error(f"handler error event_type={event_type} err={exc!r}")
        return 0

    if envelope is None:
        return 0
    _publish(envelope, session)

    if ce_type == "bloodbank.v1.agent.session.ended":
        session.archive(SESSIONS_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
