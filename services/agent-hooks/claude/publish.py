#!/usr/bin/env python3
"""Claude Code → Bloodbank hook publisher.

Reads a Claude Code hook payload from stdin, builds a CloudEvents 1.0
envelope (with correlationid + causationid linked through SessionState),
and publishes to NATS at ``event.agent.<*>``. Fire-and-forget; never
fails the agent.

Usage (from .claude/settings.json):
    python3 .../agent-hooks/claude/publish.py <event-type> [end-reason]

Event types:
    session-start, session-end, prompt-submitted,
    tool-request, tool-action, subagent-stopped

Session state lives at ~/.claude/bloodbank-session.json (single global
session across every Claude Code invocation, regardless of cwd) and is
archived to ~/.claude/bloodbank-sessions/<session_id>.json on session-end.
Per-event git/working-directory context is captured live from cwd.
"""
from __future__ import annotations

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
CLAUDE_DOMAIN = "agent"

# event-type CLI arg → (CloudEvents type, NATS subject)
EVENT_MAP: dict[str, tuple[str, str]] = {
    "session-start":    ("agent.session.started",    "event.agent.session.started"),
    "session-end":      ("agent.session.ended",      "event.agent.session.ended"),
    "prompt-submitted": ("agent.prompt.submitted",   "event.agent.prompt.submitted"),
    "tool-request":     ("agent.tool.requested",     "event.agent.tool.requested"),
    "tool-action":      ("agent.tool.invoked",       "event.agent.tool.invoked"),
    "subagent-stopped": ("agent.subagent.completed", "event.agent.subagent.completed"),
}


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


def _build(
    session: SessionState,
    ce_type: str,
    subject: str,
    data: dict[str, Any],
    *,
    event_id: str | None = None,
) -> dict[str, Any]:
    return build_envelope(
        ce_type=ce_type,
        subject=subject,
        source=CLAUDE_SOURCE,
        producer=CLAUDE_PRODUCER,
        service=CLAUDE_SERVICE,
        domain=CLAUDE_DOMAIN,
        data=data,
        correlation_id=session.session_id,
        causation_id=session.last_event_id,
        event_id=event_id,
    )


def _handle_session_start(session: SessionState) -> tuple[dict, str]:
    session.reset()
    cwd = os.getcwd()
    data = {
        "session_id": session.session_id,
        "working_directory": cwd,
        "git_branch": git_branch(cwd),
        "git_remote": git_remote(cwd),
        "started_at": session.started_at,
    }
    envelope = _build(
        session,
        "agent.session.started",
        f"agent/{session.session_id}",
        data,
        # First event id == session id keeps the chain self-rooting clean.
        event_id=session.session_id,
    )
    return envelope, "event.agent.session.started"


def _handle_prompt_submitted(session: SessionState, payload: dict) -> tuple[dict, str]:
    prompt_text = str(payload.get("prompt", ""))
    cwd = os.getcwd()
    data = {
        "session_id": session.session_id,
        "prompt_text": prompt_text,
        "prompt_length": len(prompt_text.encode("utf-8")),
        "working_directory": cwd,
        "git_branch": git_branch(cwd),
    }
    envelope = _build(
        session,
        "agent.prompt.submitted",
        f"agent/{session.session_id}",
        data,
    )
    return envelope, "event.agent.prompt.submitted"


def _handle_tool_requested(session: SessionState, payload: dict) -> tuple[dict, str]:
    tool_name = str(payload.get("tool_name", "unknown"))
    tool_input = payload.get("tool_input") or {}
    cwd = os.getcwd()
    data = {
        "session_id": session.session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "working_directory": cwd,
        "git_branch": git_branch(cwd),
        # Don't bump_tool here — the matching PostToolUse event will. Both events
        # share turn_number = current+1 so request/invoke pairs correlate.
        "turn_number": session.turn_number + 1,
    }
    envelope = _build(
        session,
        "agent.tool.requested",
        f"agent/{session.session_id}/tool/{tool_name}",
        data,
    )
    return envelope, "event.agent.tool.requested"


def _handle_tool_action(session: SessionState, payload: dict) -> tuple[dict, str]:
    tool_name = str(payload.get("tool_name", "unknown"))
    tool_input = payload.get("tool_input") or {}
    cwd = os.getcwd()
    data = {
        "session_id": session.session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "working_directory": cwd,
        "git_branch": git_branch(cwd),
        "git_status": git_status_word(cwd),
        "turn_number": session.turn_number + 1,
        "success": True,
    }
    envelope = _build(
        session,
        "agent.tool.invoked",
        f"agent/{session.session_id}/tool/{tool_name}",
        data,
    )
    return envelope, "event.agent.tool.invoked"


def _handle_subagent_completed(session: SessionState) -> tuple[dict, str]:
    data = {
        "session_id": session.session_id,
        # Claude Code's SubagentStop hook doesn't surface a structured stop
        # reason; default to "completed".
        "stop_reason": "completed",
        "working_directory": os.getcwd(),
    }
    envelope = _build(
        session,
        "agent.subagent.completed",
        f"agent/{session.session_id}/subagent",
        data,
    )
    return envelope, "event.agent.subagent.completed"


def _handle_session_end(
    session: SessionState, end_reason: str
) -> tuple[dict, str] | None:
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
    envelope = _build(
        session,
        "agent.session.ended",
        f"agent/{session.session_id}",
        data,
    )
    return envelope, "event.agent.session.ended"


def _publish(envelope: dict, subject: str, session: SessionState) -> None:
    if os.environ.get("BLOODBANK_ENABLED", "true") != "true":
        return
    body = json.dumps(envelope).encode("utf-8")
    try:
        nats_publish(subject, body, client_name="claude-events-bridge")
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
    if event_type not in EVENT_MAP:
        _log_error(f"unknown event-type: {event_type}")
        return 0

    session = SessionState(path=SESSION_FILE)
    payload = _read_stdin()

    try:
        if event_type == "session-start":
            result: tuple[dict, str] | None = _handle_session_start(session)
        elif event_type == "session-end":
            end_reason = argv[2] if len(argv) > 2 else "user_stop"
            result = _handle_session_end(session, end_reason)
        elif event_type == "prompt-submitted":
            result = _handle_prompt_submitted(session, payload)
        elif event_type == "tool-request":
            result = _handle_tool_requested(session, payload)
        elif event_type == "tool-action":
            result = _handle_tool_action(session, payload)
            session.bump_tool(str(payload.get("tool_name", "unknown")))
        elif event_type == "subagent-stopped":
            result = _handle_subagent_completed(session)
        else:
            return 0
    except Exception as exc:  # never break the agent
        _log_error(f"handler error event_type={event_type} err={exc!r}")
        return 0

    if result is None:
        return 0
    envelope, subject = result
    _publish(envelope, subject, session)

    if event_type == "session-end":
        session.archive(SESSIONS_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
