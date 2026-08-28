"""Antigravity CLI adapter for the canonical Bloodbank hook publisher.

Antigravity (Gemini CLI fork) fires named-bundle hooks from
``~/.gemini/config/hooks.json`` with camelCase (protojson) payloads:

  * common: conversationId, workspacePaths[], transcriptPath, modelName
  * PreInvocation/PostInvocation: + invocationNum, initialNumSteps
  * PostToolUse: + stepIdx, error?   (NO toolCall — tool name/args unavailable)
  * Stop: + terminationReason, fullyIdle, error?

PreToolUse is deliberately unbound (its hooks gate tool permissions — an
observational hook could block or auto-allow user tools), and no payload
carries the user prompt, so there is no prompt_submit surface.

Hooks run with cwd set to the hooks.json directory (~/.gemini/config), so the
workspace is taken from ``workspacePaths[0]``, never os.getcwd().
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from core.session import (
    SessionState,
    git_branch,
    git_commits_since,
    git_files_modified,
    git_status_word,
)

from .base import ClientAdapter

_AGY_STATE_DIR = Path.home() / ".gemini" / "antigravity-cli"


class AntigravityAdapter(ClientAdapter):
    name = "antigravity"
    source = "urn:33god:agent:antigravity-cli"
    producer = "antigravity-cli"
    service = "antigravity-hooks"
    actor_base = {
        "type": "agent_cli",
        "agent_id": "bloodbank.agent.antigravity",
        "cli": "antigravity",
        "provider": "google",
        "model": None,
    }
    nats_client_name = "antigravity-hooks-bridge"
    session_file = _AGY_STATE_DIR / "bloodbank-session.json"
    sessions_dir = _AGY_STATE_DIR / "bloodbank-sessions"
    error_log = _AGY_STATE_DIR / "bloodbank-sessions" / "publish-errors.log"

    default_map = {
        "PreInvocation": ("bloodbank.agent.invocation.started", "invocation"),
        "PostInvocation": ("bloodbank.agent.invocation.completed", "invocation"),
        "PostToolUse": ("bloodbank.agent.tool.completed", "invocation"),
        "Stop": ("bloodbank.agent.session.ended", "session"),
    }

    @property
    def agent_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "antigravity"

    def get_actor(self, payload: Any) -> dict[str, Any]:
        actor = dict(self.actor_base)
        if isinstance(payload, dict):
            model = payload.get("modelName")
            if isinstance(model, str) and model:
                actor["model"] = model
        return actor

    def shape_data(
        self,
        session: SessionState,
        ce_type: str,
        hook_name: str,
        payload: Any,
        argv: list[str],
    ) -> dict[str, Any]:
        cwd = _workspace(payload)
        raw = {"hook": hook_name, "payload": payload}

        if ce_type == "bloodbank.agent.session.ended":
            end_reason = "stop"
            if isinstance(payload, dict):
                end_reason = str(payload.get("terminationReason") or end_reason)
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
                "final_status": "error" if end_reason == "error" else "success",
                "working_directory": cwd,
                "git_branch": git_branch(cwd),
                **raw,
            }

        if ce_type == "bloodbank.agent.tool.completed":
            # PostToolUse carries only stepIdx/error — no toolCall — so the
            # tool identity is unknowable; stepIdx still gives a stable call id.
            step = _value(payload, "stepIdx")
            seed = f"{session.session_id}:{step}"
            return {
                "invocation_id": session.session_id,
                "tool_call_id": hashlib.sha1(
                    seed.encode("utf-8"), usedforsecurity=False
                ).hexdigest()[:32],
                "tool_name": "unknown",
                "outcome": "error" if _value(payload, "error") else "success",
                "working_directory": cwd,
                "git_branch": git_branch(cwd),
                "git_status": git_status_word(cwd),
                "turn_number": session.turn_number + 1,
                **raw,
            }

        if ce_type == "bloodbank.agent.invocation.started":
            return {
                "invocation_id": _invocation_id(session, payload),
                "thread_id": _value(payload, "conversationId") or session.session_id,
                "turn_id": f"{session.session_id}:{max(session.turn_number, 1)}",
                "parent_invocation_id": session.session_id,
                **raw,
            }

        if ce_type == "bloodbank.agent.invocation.completed":
            return {
                "invocation_id": _invocation_id(session, payload),
                "stop_reason": "completed",
                "working_directory": cwd,
                **raw,
            }

        return raw

    def post_publish(
        self, session: SessionState, ce_type: str, payload: Any, argv: list[str]
    ) -> None:
        if ce_type == "bloodbank.agent.tool.completed":
            session.bump_tool("unknown")
        if ce_type == "bloodbank.agent.session.ended" and self.sessions_dir:
            session.archive(self.sessions_dir)


def _value(payload: Any, *keys: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _workspace(payload: Any) -> str:
    """Hook cwd is the hooks.json dir; the real workspace rides in the payload."""
    if isinstance(payload, dict):
        paths = payload.get("workspacePaths")
        if isinstance(paths, list) and paths and isinstance(paths[0], str):
            return paths[0]
    return str(Path.home())


def _invocation_id(session: SessionState, payload: Any) -> str:
    """Pre/PostInvocation share invocationNum, so both sides of one model call
    derive the same invocation id."""
    num = _value(payload, "invocationNum")
    if num is not None:
        return f"{session.session_id}:inv{num}"
    return session.session_id
