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
        "sessionStart": ("bloodbank.v1.agent.session.started", "session"),
        "sessionEnd": ("bloodbank.v1.agent.session.ended", "session"),
        "userPromptSubmitted": ("bloodbank.v1.conversation.turn.started", "thread"),
        "preToolUse": ("bloodbank.v1.agent.tool.requested", "invocation"),
        "postToolUse": ("bloodbank.v1.agent.tool.completed", "invocation"),
        "errorOccurred": ("bloodbank.v1.agent.invocation.failed", "invocation"),
        "agentStop": ("bloodbank.v1.agent.invocation.completed", "invocation"),
    }

    @property
    def agent_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "copilot"

    def should_reset_session(self, ce_type: str, hook_name: str) -> bool:
        return hook_name == "sessionStart"

    def get_event_id(
        self, session: SessionState, ce_type: str, correlation_id: str
    ) -> str | None:
        if ce_type == "bloodbank.v1.agent.session.started":
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

        if ce_type == "bloodbank.v1.agent.session.started":
            return {"session_id": session_id, **raw}

        if ce_type == "bloodbank.v1.agent.session.ended":
            end_reason = None
            if isinstance(payload, dict):
                end_reason = payload.get("reason") or payload.get("end_reason")
            return {"session_id": session_id, "end_reason": end_reason, **raw}

        if ce_type == "bloodbank.v1.conversation.turn.started":
            prompt_text = None
            if isinstance(payload, dict):
                prompt_text = payload.get("prompt") or payload.get("prompt_text")
            return {
                "thread_id": session_id,
                "turn_id": session_id,
                "prompt_text": prompt_text,
                **raw,
            }

        if ce_type.startswith("bloodbank.v1.agent.tool."):
            tool_name = "unknown"
            arguments: dict[str, Any] | None = None
            if isinstance(payload, dict):
                tool_name = str(
                    payload.get("tool") or payload.get("tool_name") or "unknown"
                )
                args = payload.get("arguments") or payload.get("tool_input")
                if isinstance(args, dict):
                    arguments = args
            base: dict[str, Any] = {
                "invocation_id": session_id,
                "tool_call_id": _tool_call_id(session_id, hook_name, payload),
                "tool_name": tool_name,
                **raw,
            }
            if arguments is not None:
                base["arguments"] = arguments
            if ce_type == "bloodbank.v1.agent.tool.completed":
                outcome = "success"
                if isinstance(payload, dict) and (
                    payload.get("is_error") or payload.get("error")
                ):
                    outcome = "error"
                base["outcome"] = outcome
            return base

        if ce_type == "bloodbank.v1.agent.invocation.failed":
            err_msg = None
            err_code = None
            if isinstance(payload, dict):
                err_msg = payload.get("message") or payload.get("error")
                err_code = payload.get("code")
            return {
                "invocation_id": session_id,
                "error_code": err_code,
                "error_message": err_msg,
                **raw,
            }

        if ce_type == "bloodbank.v1.agent.invocation.completed":
            return {"invocation_id": session_id, **raw}

        return raw


def _tool_call_id(session_id: str, hook_name: str, payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("tool_call_id", "toolUseId", "id"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                return v
    seed = json.dumps([session_id, hook_name, payload], sort_keys=True, default=str)
    return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:32]
