"""Claude Code adapter for the canonical Bloodbank hook publisher.

Encapsulates Claude Code-specific payload reading, data shaping, session
paths, and actor defaults.  Behavioral source of truth: the original
``claude/publish.py`` (now a thin wrapper).
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from core.session import (
    SessionState,
    git_branch,
    git_commits_since,
    git_files_modified,
    git_remote,
    git_status_word,
)

from .base import ClientAdapter

_CLAUDE_STATE_DIR = Path.home() / ".claude"


class ClaudeAdapter(ClientAdapter):
    name = "claude"
    source = "urn:33god:agent:claude-code"
    producer = "claude-code"
    service = "claude-code"
    actor_base = {
        "type": "agent_cli",
        "agent_id": "bloodbank.agent.claude",
        "cli": "claude",
        "provider": "anthropic",
        "model": None,
    }
    nats_client_name = "agent-hooks-claude"
    session_file = _CLAUDE_STATE_DIR / "bloodbank-session.json"
    sessions_dir = _CLAUDE_STATE_DIR / "bloodbank-sessions"
    error_log = _CLAUDE_STATE_DIR / "bloodbank-sessions" / "publish-errors.log"

    default_map = {
        "session-start": ("bloodbank.v1.agent.session.started", "session"),
        "session-end": ("bloodbank.v1.agent.session.ended", "session"),
        "session-stop": ("bloodbank.v1.agent.session.ended", "session"),
        "prompt-submitted": ("bloodbank.v1.conversation.turn.started", "thread"),
        "tool-request": ("bloodbank.v1.agent.tool.requested", "invocation"),
        "tool-action": ("bloodbank.v1.agent.tool.completed", "invocation"),
        "subagent-stopped": ("bloodbank.v1.agent.invocation.completed", "invocation"),
    }

    @property
    def agent_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "claude"

    def shape_data(
        self,
        session: SessionState,
        ce_type: str,
        hook_name: str,
        payload: Any,
        argv: list[str],
    ) -> dict[str, Any]:
        cwd = os.getcwd()

        if ce_type == "bloodbank.v1.agent.session.started":
            return {
                "session_id": session.session_id,
                "working_directory": cwd,
                "git_branch": git_branch(cwd),
                "git_remote": git_remote(cwd),
                "started_at": session.started_at,
            }

        if ce_type == "bloodbank.v1.agent.session.ended":
            end_reason = argv[2] if len(argv) > 2 else "user_stop"
            try:
                started = datetime.fromisoformat(
                    session.started_at.replace("Z", "+00:00")
                )
                duration = int(
                    (datetime.now(started.tzinfo) - started).total_seconds()
                )
            except (ValueError, TypeError):
                duration = 0
            return {
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

        if ce_type == "bloodbank.v1.conversation.turn.started":
            prompt_text = str(payload.get("prompt", ""))
            turn_id = f"{session.session_id}:{session.turn_number + 1}"
            return {
                "thread_id": session.session_id,
                "turn_id": turn_id,
                "prompt_text": prompt_text,
                "prompt_length": len(prompt_text.encode("utf-8")),
                "working_directory": cwd,
                "git_branch": git_branch(cwd),
            }

        if ce_type == "bloodbank.v1.agent.tool.requested":
            tool_name = str(payload.get("tool_name", "unknown"))
            tool_input = payload.get("tool_input") or {}
            turn_number = session.turn_number + 1
            return {
                "invocation_id": session.session_id,
                "tool_call_id": _tool_call_id(session, tool_name, turn_number),
                "tool_name": tool_name,
                "arguments": tool_input,
                "working_directory": cwd,
                "git_branch": git_branch(cwd),
                "turn_number": turn_number,
            }

        if ce_type == "bloodbank.v1.agent.tool.completed":
            tool_name = str(payload.get("tool_name", "unknown"))
            tool_input = payload.get("tool_input") or {}
            turn_number = session.turn_number + 1
            return {
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

        if ce_type == "bloodbank.v1.agent.invocation.completed":
            return {
                "invocation_id": session.session_id,
                "stop_reason": "completed",
                "working_directory": cwd,
            }

        return {"hook": hook_name, "payload": payload}

    def before_publish(
        self, session: SessionState, ce_type: str, payload: Any, argv: list[str]
    ) -> None:
        if ce_type == "bloodbank.v1.agent.tool.completed":
            session.bump_tool(str(payload.get("tool_name", "unknown")))

    def post_publish(
        self, session: SessionState, ce_type: str, payload: Any, argv: list[str]
    ) -> None:
        return None

    def after_publish_attempt(
        self,
        session: SessionState,
        ce_type: str,
        payload: Any,
        argv: list[str],
        *,
        published: bool,
    ) -> None:
        if ce_type == "bloodbank.v1.agent.session.ended" and self.sessions_dir:
            session.archive(self.sessions_dir)


def _tool_call_id(session: SessionState, tool_name: str, turn_number: int) -> str:
    seed = f"{session.session_id}:{turn_number}:{tool_name}"
    return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:32]


def _tool_outcome(payload: dict) -> str:
    if isinstance(payload, dict):
        if payload.get("is_error"):
            return "error"
        resp = payload.get("tool_response") or payload.get("tool_result")
        if isinstance(resp, dict) and (resp.get("error") or resp.get("is_error")):
            return "error"
    return "success"
