"""GitHub Copilot CLI adapter for the canonical Bloodbank hook publisher.

Encapsulates Copilot-specific session paths, data shaping, and hook-name
resolution.  Behavioral source of truth: the original ``copilot/publish.py``
(now a thin wrapper).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from core.session import SessionState

from .base import ClientAdapter


class CopilotAdapter(ClientAdapter):
    name = "copilot"
    source = "urn:33god:integration:copilot-cli"
    producer = "copilot-cli"
    service = "copilot-hooks"
    actor_base = {
        "type": "agent_cli",
        "agent_id": "bloodbank.agent.copilot",
        "cli": "copilot",
        "provider": "github_copilot",
        "model": None,
    }
    nats_client_name = "copilot-hooks-bridge"
    session_file = Path.home() / ".copilot" / "bloodbank-session.json"
    sessions_dir = None
    error_log = None

    default_map = {
        "sessionStart": ("bloodbank.agent.session.started", "session"),
        "sessionEnd": ("bloodbank.agent.session.ended", "session"),
        "userPromptSubmitted": ("bloodbank.conversation.turn.started", "thread"),
        "preToolUse": ("bloodbank.agent.tool.requested", "invocation"),
        "postToolUse": ("bloodbank.agent.tool.completed", "invocation"),
        "errorOccurred": ("bloodbank.agent.invocation.failed", "invocation"),
        "agentStop": ("bloodbank.conversation.turn.completed", "thread"),
        "subagentStart": ("bloodbank.agent.invocation.started", "invocation"),
        "subagentStop": ("bloodbank.agent.invocation.completed", "invocation"),
        "postToolUseFailure": ("bloodbank.agent.tool.completed", "invocation"),
    }

    @property
    def agent_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "copilot"

    def should_reset_session(self, ce_type: str, hook_name: str) -> bool:
        return hook_name == "sessionStart"

    def get_event_id(
        self, session: SessionState, ce_type: str, correlation_id: str
    ) -> str | None:
        if ce_type == "bloodbank.agent.session.started":
            return correlation_id
        return None

    def shape_data(
        self,
        session: SessionState,
        ce_type: str,
        hook_name: str,
        payload: Any,
        argv: list[str],
    ) -> dict[str, Any]:
        session_id = session.session_id
        raw = {"hook": hook_name, "payload": payload}
        # Same gap as hermes: copilot carries the agent's cwd in the payload but
        # never promoted it, so every copilot event was unattributable to a repo.
        cwd = ""
        if isinstance(payload, dict):
            cwd = str(payload.get("cwd") or payload.get("working_directory") or "")
        cwd = cwd or os.getcwd()

        if ce_type == "bloodbank.agent.session.started":
            return {"session_id": session_id, "working_directory": cwd, **raw}

        if ce_type == "bloodbank.agent.session.ended":
            end_reason = None
            if isinstance(payload, dict):
                end_reason = payload.get("reason") or payload.get("end_reason")
            return {"session_id": session_id, "end_reason": end_reason,
                    "working_directory": cwd, **raw}

        if ce_type == "bloodbank.conversation.turn.started":
            prompt_text = None
            if isinstance(payload, dict):
                prompt_text = payload.get("prompt") or payload.get("prompt_text")
            return {
                "thread_id": session_id,
                "turn_id": session_id,
                "prompt_text": prompt_text,
                "working_directory": cwd,
                **raw,
            }

        if ce_type == "bloodbank.conversation.turn.completed":
            return {"thread_id": session_id,
                    "turn_id": str(payload.get("turnId") or payload.get("turn_id") or session_id),
                    "outcome": "failed" if payload.get("error") else "completed",
                    "working_directory": cwd, **raw}

        if ce_type.startswith("bloodbank.agent.tool."):
            tool_name = "unknown"
            arguments: dict[str, Any] | None = None
            if isinstance(payload, dict):
                tool_name = str(
                    payload.get("toolName") or payload.get("tool") or payload.get("tool_name") or "unknown"
                )
                args = next((payload[key] for key in ("toolArgs", "arguments", "tool_input")
                             if key in payload), None)
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (ValueError, TypeError):
                        pass
                if isinstance(args, dict):
                    arguments = args
            base: dict[str, Any] = {
                "invocation_id": session_id,
                "tool_call_id": _tool_call_id(session_id, hook_name, payload),
                "tool_name": tool_name,
                "working_directory": cwd,
                **raw,
            }
            if arguments is not None:
                base["arguments"] = arguments
            if ce_type == "bloodbank.agent.tool.completed":
                outcome = "success"
                if isinstance(payload, dict) and (
                    payload.get("is_error") or payload.get("error") or hook_name == "postToolUseFailure"
                ):
                    outcome = "error"
                base["outcome"] = outcome
                if isinstance(payload, dict):
                    result = payload.get("toolResult", payload.get("tool_result"))
                    if result is not None:
                        base["result"] = result
            return base

        if ce_type == "bloodbank.agent.invocation.failed":
            err_msg = None
            err_code = None
            if isinstance(payload, dict):
                err_msg = payload.get("message") or payload.get("error")
                err_code = payload.get("code")
                if isinstance(err_msg, dict):
                    err_code = err_code or err_msg.get("code") or err_msg.get("name")
                    err_msg = err_msg.get("message")
            return {
                "invocation_id": session_id,
                "error_code": str(err_code) if err_code is not None else None,
                "error_message": str(err_msg) if err_msg is not None else None,
                **raw,
            }

        if ce_type == "bloodbank.agent.invocation.completed":
            return {"invocation_id": str(payload.get("agentId") or session_id), **raw}

        if ce_type == "bloodbank.agent.invocation.started":
            return {"invocation_id": str(payload.get("agentId") or session_id),
                    "parent_invocation_id": session_id, "working_directory": cwd, **raw}

        return raw


def _tool_call_id(session_id: str, hook_name: str, payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("toolCallId", "tool_call_id", "toolUseId", "id"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                return v
    seed = json.dumps([session_id, hook_name, payload], sort_keys=True, default=str)
    return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:32]
