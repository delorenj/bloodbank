#!/usr/bin/env python3
"""Hermes agent → Bloodbank hook publisher (v1 contract).

Hermes-agent fires shell hooks (configured in its `hooks:` block in
config.yaml; see agent/shell_hooks.py). Each hook command is run with
``shell=False`` (shlex argv) and the hook payload piped as JSON on stdin:
``{"hook_event_name": "<event>", "session_id": ..., "tool_name": ..., ...}``.

Usage (from config.yaml hooks: block):
    python3 .../agent-hooks/hermes/publish.py <hermes-event>

Hermes event → v1 CloudEvents type (canonical; sourced from
hermes/event_map.generated.json with the embedded table as fallback):
    on_session_start -> bloodbank.v1.agent.session.started
    on_session_end   -> bloodbank.v1.agent.session.ended
    pre_tool_call    -> bloodbank.v1.agent.tool.requested
    post_tool_call   -> bloodbank.v1.agent.tool.completed
    subagent_stop    -> bloodbank.v1.agent.invocation.completed

Fire-and-forget; never blocks Hermes. Correlation uses the Hermes-provided
session_id when present so the chain matches Hermes' own session.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from core.envelope import build_envelope  # noqa: E402
from core.event_map import resolve_map  # noqa: E402
from core.nats_publish import publish as nats_publish  # noqa: E402
from core.session import SessionState  # noqa: E402

HERMES_STATE_DIR = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
SESSION_FILE = HERMES_STATE_DIR / "bloodbank-session.json"

HERMES_SOURCE = "urn:33god:agent:hermes"
HERMES_PRODUCER = "hermes-agent"
HERMES_SERVICE = "hermes-hooks"
HERMES_ACTOR: dict[str, Any] = {
    "type": "agent_cli",
    "agent_id": "bloodbank.agent.hermes",
    "cli": "hermes",
    "provider": None,
    "model": None,
}

# Embedded fallback; the active map is merged over hermes/event_map.generated.json
# (projected from hooks.master.json by sync.py). Run `mise run hooks:sync`.
_DEFAULT_MAP: dict[str, tuple[str, str]] = {
    "on_session_start": ("bloodbank.v1.agent.session.started", "session"),
    "on_session_end": ("bloodbank.v1.agent.session.ended", "session"),
    "pre_tool_call": ("bloodbank.v1.agent.tool.requested", "invocation"),
    "post_tool_call": ("bloodbank.v1.agent.tool.completed", "invocation"),
    "subagent_stop": ("bloodbank.v1.agent.invocation.completed", "invocation"),
}

HOOK_MAP: dict[str, tuple[str, str]] = resolve_map(
    Path(__file__).resolve().parent, _DEFAULT_MAP
)


def _log(msg: str) -> None:
    if os.environ.get("BLOODBANK_DEBUG") == "true" or os.environ.get("BLOODBANK_HOOK_VERBOSE"):
        print(f"[bloodbank-hermes-hook] {msg}", file=sys.stderr)


def _read_payload() -> dict[str, Any]:
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


def _event_name(argv: list[str], payload: dict) -> str | None:
    if len(argv) > 1 and argv[1].strip():
        return argv[1].strip()
    v = payload.get("hook_event_name")
    return v.strip() if isinstance(v, str) and v.strip() else None


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
    return "success"


def _shape_data(ce_type: str, session_id: str, event: str, payload: dict) -> dict[str, Any]:
    raw = {"hook": event, "payload": payload}
    if ce_type == "bloodbank.v1.agent.session.started":
        return {"session_id": session_id, "working_directory": os.getcwd(), **raw}
    if ce_type == "bloodbank.v1.agent.session.ended":
        return {"session_id": session_id, "end_reason": _value(payload, "reason", "end_reason"), **raw}
    if ce_type == "bloodbank.v1.agent.tool.requested":
        return {
            "invocation_id": session_id,
            "tool_call_id": _tool_call_id(session_id, payload),
            "tool_name": _tool_name(payload),
            "arguments": _value(payload, "tool_input", "arguments") or {},
            **raw,
        }
    if ce_type == "bloodbank.v1.agent.tool.completed":
        return {
            "invocation_id": session_id,
            "tool_call_id": _tool_call_id(session_id, payload),
            "tool_name": _tool_name(payload),
            "outcome": _outcome(payload),
            **raw,
        }
    if ce_type == "bloodbank.v1.agent.invocation.completed":
        return {"invocation_id": session_id, "stop_reason": _value(payload, "reason", "stop_reason") or "completed", **raw}
    return raw


def main(argv: list[str]) -> int:
    payload = _read_payload()
    event = _event_name(argv, payload)
    if not event:
        print("usage: publish.py <hermes-event>", file=sys.stderr)
        return 2
    mapping = HOOK_MAP.get(event)
    if mapping is None:
        _log(f"unsupported hermes event (ignored): {event}")
        return 1 if os.environ.get("BLOODBANK_HOOK_STRICT") == "1" else 0
    ce_type, bucket = mapping

    session = SessionState(path=SESSION_FILE)
    # Prefer Hermes' own session id for correlation; fall back to local state.
    hermes_sid = _value(payload, "session_id", "sessionId")
    if ce_type == "bloodbank.v1.agent.session.started" and not hermes_sid:
        session.reset()
    correlation = str(hermes_sid) if hermes_sid else session.session_id

    actor = dict(HERMES_ACTOR)
    actor["model"] = _model(payload)

    try:
        data = _shape_data(ce_type, correlation, event, payload)
        envelope = build_envelope(
            ce_type=ce_type,
            kind="event",
            source=HERMES_SOURCE,
            producer=HERMES_PRODUCER,
            service=HERMES_SERVICE,
            actor=actor,
            data=data,
            correlation_id=correlation,
            causation_id=session.last_event_id or correlation,
            ordering_key=f"{bucket}:{correlation}",
            event_id=correlation if ce_type == "bloodbank.v1.agent.session.started" and not hermes_sid else None,
        )
    except Exception as exc:  # never block hermes
        _log(f"envelope build failed event={event} type={ce_type} err={exc!r}")
        return 1 if os.environ.get("BLOODBANK_HOOK_STRICT") == "1" else 0

    if os.environ.get("BLOODBANK_ENABLED", "true") != "true":
        return 0
    subject = envelope["subject"]
    try:
        nats_publish(subject, json.dumps(envelope).encode("utf-8"), client_name="hermes-hooks-bridge")
    except (OSError, RuntimeError) as exc:
        _log(f"publish failed ({subject}): {exc}")
        return 1 if os.environ.get("BLOODBANK_HOOK_STRICT") == "1" else 0
    session.record_event(envelope["id"])
    _log(f"published {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
