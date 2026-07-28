"""Codex CLI adapter for the canonical Bloodbank hook publisher.

Encapsulates Codex-specific payload reading (stdin or argv[2]), hook-name
resolution (argv or payload field), legacy alias support, model extraction,
and data shaping.  Behavioral source of truth: the original
``codex/publish.py`` (now a thin wrapper).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
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

_CODEX_STATE_DIR = Path.home() / ".codex"


class CodexAdapter(ClientAdapter):
    name = "codex"
    source = "urn:33god:agent:codex-cli"
    producer = "codex-cli"
    service = "codex-hooks"
    actor_base = {
        "type": "agent_cli",
        "agent_id": "bloodbank.agent.codex",
        "cli": "codex",
        "provider": "openai",
        "model": None,
    }
    nats_client_name = "codex-hooks-bridge"
    session_file = _CODEX_STATE_DIR / "bloodbank-session.json"
    sessions_dir = _CODEX_STATE_DIR / "bloodbank-sessions"
    error_log = _CODEX_STATE_DIR / "bloodbank-sessions" / "publish-errors.log"

    default_map = {
        "SessionStart": ("bloodbank.v1.agent.session.started", "session"),
        "session-start": ("bloodbank.v1.agent.session.started", "session"),
        "Stop": ("bloodbank.v1.agent.session.ended", "session"),
        "SessionEnd": ("bloodbank.v1.agent.session.ended", "session"),
        "session-end": ("bloodbank.v1.agent.session.ended", "session"),
        "UserPromptSubmit": ("bloodbank.v1.conversation.turn.started", "thread"),
        "prompt-submitted": ("bloodbank.v1.conversation.turn.started", "thread"),
        "PreToolUse": ("bloodbank.v1.agent.tool.requested", "invocation"),
        "tool-request": ("bloodbank.v1.agent.tool.requested", "invocation"),
        "PostToolUse": ("bloodbank.v1.agent.tool.completed", "invocation"),
        "tool-action": ("bloodbank.v1.agent.tool.completed", "invocation"),
        "tool-completed": ("bloodbank.v1.agent.tool.completed", "invocation"),
        "SubagentStart": ("bloodbank.v1.agent.invocation.started", "invocation"),
        "subagent-started": ("bloodbank.v1.agent.invocation.started", "invocation"),
        "SubagentStop": ("bloodbank.v1.agent.invocation.completed", "invocation"),
        "subagent-stopped": ("bloodbank.v1.agent.invocation.completed", "invocation"),
        "notify": ("bloodbank.v1.conversation.turn.completed", "thread"),
    }

    @property
    def agent_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "codex"

    def read_payload(self, argv: list[str]) -> Any:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                return _parse_json(raw)
        if len(argv) > 2:
            return _parse_json(argv[2])
        return {}

    def resolve_hook_name(self, argv: list[str], payload: Any) -> str | None:
        if len(argv) > 1 and argv[1].strip():
            return argv[1].strip()
        if isinstance(payload, dict):
            value = payload.get("hook_event_name") or payload.get("hookEventName")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def get_actor(self, payload: Any) -> dict[str, Any]:
        actor = dict(self.actor_base)
        actor["model"] = _payload_model(payload)
        return actor

    def shape_data(
        self,
        session: SessionState,
        ce_type: str,
        hook_name: str,
        payload: Any,
        argv: list[str],
    ) -> dict[str, Any]:
        cwd = os.getcwd()
        raw = {"hook": hook_name, "payload": payload}

        if ce_type == "bloodbank.v1.agent.session.started":
            return {
                "session_id": session.session_id,
                "working_directory": cwd,
                "git_branch": git_branch(cwd),
                "git_remote": git_remote(cwd),
                "started_at": session.started_at,
                **raw,
            }

        if ce_type == "bloodbank.v1.agent.session.ended":
            end_reason = "user_stop"
            if hook_name == "session-end" and isinstance(payload, str) and payload:
                end_reason = payload
            if isinstance(payload, dict):
                end_reason = str(
                    payload.get("reason") or payload.get("end_reason") or end_reason
                )
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
                "final_status": "success" if end_reason != "error" else "error",
                "working_directory": cwd,
                "git_branch": git_branch(cwd),
                **raw,
            }

        if ce_type == "bloodbank.v1.conversation.turn.started":
            prompt_text = str(
                _value(payload, "prompt", "user_prompt", "userPrompt") or ""
            )
            return {
                "thread_id": session.session_id,
                "turn_id": _turn_id(session, payload),
                "prompt_text": prompt_text,
                "prompt_length": len(prompt_text.encode("utf-8")),
                "working_directory": cwd,
                "git_branch": git_branch(cwd),
                **raw,
            }

        if ce_type == "bloodbank.v1.conversation.turn.completed":
            outcome = "completed" if _tool_outcome(payload) == "success" else "failed"
            return {
                "thread_id": session.session_id,
                "turn_id": _turn_id(session, payload),
                "outcome": outcome,
                **raw,
            }

        if ce_type == "bloodbank.v1.agent.tool.requested":
            return {
                "invocation_id": _invocation_id(session, payload),
                "tool_call_id": _tool_call_id(session, payload),
                "tool_name": _tool_name(payload),
                "arguments": _tool_arguments(payload),
                "working_directory": cwd,
                "git_branch": git_branch(cwd),
                "turn_number": session.turn_number + 1,
                **raw,
            }

        if ce_type == "bloodbank.v1.agent.tool.completed":
            data: dict[str, Any] = {
                "invocation_id": _invocation_id(session, payload),
                "tool_call_id": _tool_call_id(session, payload),
                "tool_name": _tool_name(payload),
                "arguments": _tool_arguments(payload),
                "outcome": _tool_outcome(payload),
                "working_directory": cwd,
                "git_branch": git_branch(cwd),
                "git_status": git_status_word(cwd),
                "turn_number": session.turn_number + 1,
                **raw,
            }
            result = _tool_result(payload)
            if result is not None:
                data["result"] = result
            return data

        if ce_type == "bloodbank.v1.agent.invocation.started":
            return {
                "invocation_id": _invocation_id(session, payload),
                "thread_id": _value(payload, "thread_id", "threadId")
                or session.session_id,
                "turn_id": _turn_id(session, payload),
                "parent_invocation_id": session.session_id,
                **raw,
            }

        if ce_type == "bloodbank.v1.agent.invocation.completed":
            return {
                "invocation_id": _invocation_id(session, payload),
                "stop_reason": _value(payload, "reason", "stop_reason", "stopReason")
                or "completed",
                "working_directory": cwd,
                **raw,
            }

        return raw

    def post_publish(
        self, session: SessionState, ce_type: str, payload: Any, argv: list[str]
    ) -> None:
        if ce_type == "bloodbank.v1.agent.tool.completed":
            session.bump_tool(_tool_name(payload))
        if ce_type == "bloodbank.v1.agent.session.ended" and self.sessions_dir:
            session.archive(self.sessions_dir)


def _parse_json(raw: str) -> Any:
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _value(payload: Any, *keys: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _payload_model(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("model", "model_name", "modelName"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return os.environ.get("CODEX_MODEL") or None


def _tool_name(payload: Any) -> str:
    value = _value(payload, "tool_name", "toolName", "tool", "name", "command")
    if isinstance(value, dict):
        value = value.get("name")
    return str(value or "unknown")


def _tool_arguments(payload: Any) -> dict[str, Any]:
    value = _value(payload, "tool_input", "toolInput", "arguments", "args", "input")
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"value": value}


def _tool_result(payload: Any) -> Any:
    return _value(payload, "tool_output", "toolOutput", "result", "output")


def _tool_outcome(payload: Any) -> str:
    if isinstance(payload, dict):
        if payload.get("is_error") or payload.get("error"):
            return "error"
        if str(payload.get("status", "")).lower() in {"error", "failed", "failure"}:
            return "error"
        if payload.get("exit_code") not in (None, 0, "0"):
            return "error"
    return "success"


def _tool_call_id(session: SessionState, payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("tool_call_id", "toolCallId", "toolUseId", "call_id", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    seed = f"{session.session_id}:{session.turn_number + 1}:{_tool_name(payload)}"
    return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:32]


def _invocation_id(session: SessionState, payload: Any) -> str:
    value = _value(
        payload,
        "invocation_id",
        "invocationId",
        "agent_thread_id",
        "agentThreadId",
        "thread_id",
        "threadId",
    )
    return str(value or session.session_id)


def _turn_id(session: SessionState, payload: Any) -> str:
    value = _value(payload, "turn_id", "turnId")
    if value:
        return str(value)
    turn_number = max(session.turn_number, 1)
    return f"{session.session_id}:{turn_number}"
