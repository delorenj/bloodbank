"""Hermes agent adapter for the canonical Bloodbank hook publisher.

Encapsulates Hermes-specific payload flattening (lifting ``extra``), session
path (HERMES_HOME-aware), correlation-id preference (Hermes-provided
session_id), and data shaping.  Behavioral source of truth: the original
``hermes/publish.py`` (now a thin wrapper).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from core.session import SessionState

from .base import ClientAdapter

_HERMES_STATE_DIR = Path(
    os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
)


class HermesAdapter(ClientAdapter):
    name = "hermes"
    source = "urn:33god:agent:hermes"
    producer = "hermes-agent"
    service = "hermes-hooks"
    actor_base = {
        "type": "agent_cli",
        "agent_id": "bloodbank.agent.hermes",
        "cli": "hermes",
        "provider": None,
        "model": None,
    }
    nats_client_name = "hermes-hooks-bridge"
    session_file = _HERMES_STATE_DIR / "bloodbank-session.json"
    sessions_dir = None
    error_log = None

    default_map = {
        "on_session_start": ("bloodbank.v1.agent.session.started", "session"),
        "on_session_end": ("bloodbank.v1.agent.session.ended", "session"),
        "pre_tool_call": ("bloodbank.v1.agent.tool.requested", "invocation"),
        "post_tool_call": ("bloodbank.v1.agent.tool.completed", "invocation"),
        "subagent_stop": ("bloodbank.v1.agent.invocation.completed", "invocation"),
    }

    @property
    def agent_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "hermes"

    def resolve_hook_name(self, argv: list[str], payload: Any) -> str | None:
        if len(argv) > 1 and argv[1].strip():
            return argv[1].strip()
        if isinstance(payload, dict):
            v = payload.get("hook_event_name")
            return v.strip() if isinstance(v, str) and v.strip() else None
        return None

    def get_correlation_id(self, session: SessionState, payload: Any) -> str:
        flat = _flatten(payload) if isinstance(payload, dict) else {}
        hermes_sid = _value(flat, "session_id", "sessionId")
        return str(hermes_sid) if hermes_sid else session.session_id

    def get_causation_id(
        self,
        session: SessionState,
        ce_type: str,
        hook_name: str,
        correlation_id: str,
    ) -> str:
        if ce_type == "bloodbank.v1.agent.session.started":
            return correlation_id
        return session.last_event_id or correlation_id

    def get_event_id(
        self, session: SessionState, ce_type: str, correlation_id: str
    ) -> str | None:
        if ce_type == "bloodbank.v1.agent.session.started":
            return correlation_id
        return None

    def get_actor(self, payload: Any) -> dict[str, Any]:
        actor = dict(self.actor_base)
        flat = _flatten(payload) if isinstance(payload, dict) else {}
        actor["model"] = _model(flat)
        return actor

    def shape_data(
        self,
        session: SessionState,
        ce_type: str,
        hook_name: str,
        payload: Any,
        argv: list[str],
    ) -> dict[str, Any]:
        flat = _flatten(payload) if isinstance(payload, dict) else {}
        correlation = self.get_correlation_id(session, payload)
        raw = {"hook": hook_name, "payload": payload}

        if ce_type == "bloodbank.v1.agent.session.started":
            return {
                "session_id": correlation,
                "working_directory": os.getcwd(),
                **raw,
            }

        if ce_type == "bloodbank.v1.agent.session.ended":
            return {
                "session_id": correlation,
                "end_reason": _value(flat, "reason", "end_reason"),
                **raw,
            }

        if ce_type == "bloodbank.v1.agent.tool.requested":
            return {
                "invocation_id": correlation,
                "tool_call_id": _tool_call_id(correlation, flat),
                "tool_name": _tool_name(flat),
                "arguments": _value(flat, "tool_input", "arguments") or {},
                **raw,
            }

        if ce_type == "bloodbank.v1.agent.tool.completed":
            return {
                "invocation_id": correlation,
                "tool_call_id": _tool_call_id(correlation, flat),
                "tool_name": _tool_name(flat),
                "outcome": _outcome(flat),
                **raw,
            }

        if ce_type == "bloodbank.v1.agent.invocation.completed":
            return {
                "invocation_id": correlation,
                "stop_reason": _value(flat, "reason", "stop_reason") or "completed",
                **raw,
            }

        return raw


def _flatten(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    merged = dict(payload.get("extra") or {})
    merged.update({k: v for k, v in payload.items() if k != "extra"})
    return merged


def _value(payload: dict, *keys: str) -> Any:
    for k in keys:
        if payload.get(k) not in (None, ""):
            return payload[k]
    return None


def _model(payload: dict) -> Any:
    return _value(payload, "model", "model_name") or os.environ.get("HERMES_MODEL") or None


def _tool_name(payload: dict) -> str:
    v = _value(payload, "tool_name", "tool", "name")
    if isinstance(v, dict):
        v = v.get("name")
    return str(v or "unknown")


def _tool_call_id(session_id: str, payload: dict) -> str:
    v = _value(payload, "tool_call_id", "tool_use_id", "id")
    if isinstance(v, str) and v:
        return v
    seed = f"{session_id}:{_tool_name(payload)}:{json.dumps(payload.get('tool_input'), sort_keys=True, default=str)}"
    return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:32]


def _outcome(payload: dict) -> str:
    if payload.get("is_error") or payload.get("error"):
        return "error"
    if str(payload.get("status", "")).lower() in {"error", "failed", "failure"}:
        return "error"
    res = payload.get("result")
    if isinstance(res, dict) and (res.get("error") or res.get("is_error")):
        return "error"
    if isinstance(res, str) and res.lstrip().startswith("{"):
        try:
            j = json.loads(res)
        except json.JSONDecodeError:
            j = None
        if isinstance(j, dict) and (j.get("error") or j.get("is_error")):
            return "error"
    return "success"
