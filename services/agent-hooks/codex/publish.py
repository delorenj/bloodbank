#!/usr/bin/env python3
"""Codex CLI -> Bloodbank hook publisher (v1 contract).

Reads a Codex hook payload from stdin, builds a CloudEvents 1.0 envelope
per bloodbank/docs/event-naming.md, and publishes to NATS at
``bloodbank.evt.v1.<domain>.<entity>.<action>``. Hook failures are
fail-open by default so Codex itself keeps running.

Usage from ~/.codex/hooks.json:
    cat | python3 .../agent-hooks/codex/publish.py <hookName>

Supported hook names (canonical; sourced from hooks.master.json via
codex/event_map.generated.json, with the embedded table as fallback):
    SessionStart      -> bloodbank.v1.agent.session.started
    Stop              -> bloodbank.v1.agent.session.ended
    UserPromptSubmit  -> bloodbank.v1.conversation.turn.started
    PreToolUse        -> bloodbank.v1.agent.tool.requested
    PostToolUse       -> bloodbank.v1.agent.tool.completed
    SubagentStart     -> bloodbank.v1.agent.invocation.started
    SubagentStop      -> bloodbank.v1.agent.invocation.completed

Edit hooks.master.json then run `mise run hooks:sync`; do not hand-edit the
map. Legacy aliases matching the earlier Claude-style wiring are accepted to
make migration from ~/.codex/hooks.json painless.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

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

CODEX_STATE_DIR = Path.home() / ".codex"
SESSION_FILE = CODEX_STATE_DIR / "bloodbank-session.json"
SESSIONS_DIR = CODEX_STATE_DIR / "bloodbank-sessions"
ERROR_LOG = SESSIONS_DIR / "publish-errors.log"

CODEX_SOURCE = "urn:33god:agent:codex-cli"
CODEX_PRODUCER = "codex-cli"
CODEX_SERVICE = "codex-hooks"
CODEX_ACTOR: dict[str, Any] = {
    "type": "agent_cli",
    "agent_id": "bloodbank.agent.codex",
    "cli": "codex",
    "provider": "openai",
    "model": None,
}

# Embedded fallback + migration aliases. The active map is sourced from
# codex/event_map.generated.json (projected from hooks.master.json by sync.py)
# and merged OVER this default, so canonical native names track the SSOT while
# the legacy aliases below keep working. Run `mise run hooks:sync` after edits.
_DEFAULT_MAP: dict[str, tuple[str, str]] = {
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
    # Legacy Codex notify hook: agent-turn-complete.
    "notify": ("bloodbank.v1.conversation.turn.completed", "thread"),
}

# Codex hook name or migration alias -> (v1 CloudEvents type, ordering bucket).
HOOK_MAP: dict[str, tuple[str, str]] = resolve_map(
    Path(__file__).resolve().parent, _DEFAULT_MAP
)


def _log(msg: str) -> None:
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
    if os.environ.get("BLOODBANK_DEBUG") == "true" or os.environ.get(
        "BLOODBANK_HOOK_VERBOSE"
    ):
        print(f"[bloodbank-codex-hook] {msg}", file=sys.stderr)


def _parse_json(raw: str) -> Any:
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _read_payload(argv: list[str]) -> Any:
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            return _parse_json(raw)
    if len(argv) > 2:
        return _parse_json(argv[2])
    return {}


def _event_name(argv: list[str], payload: Any) -> str | None:
    if len(argv) > 1 and argv[1].strip():
        return argv[1].strip()
    if isinstance(payload, dict):
        value = payload.get("hook_event_name") or payload.get("hookEventName")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _payload_model(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("model", "model_name", "modelName"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return os.environ.get("CODEX_MODEL") or None


def _actor(payload: Any) -> dict[str, Any]:
    actor = dict(CODEX_ACTOR)
    actor["model"] = _payload_model(payload)
    return actor


def _raw(hook_name: str, payload: Any) -> dict[str, Any]:
    return {"hook": hook_name, "payload": payload}


def _value(payload: Any, *keys: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


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


def _build_event(
    session: SessionState,
    ce_type: str,
    bucket_prefix: str,
    data: dict[str, Any],
    payload: Any,
    *,
    event_id: str | None = None,
) -> dict[str, Any]:
    ordering_key = f"{bucket_prefix}:{session.session_id}"
    return build_envelope(
        ce_type=ce_type,
        kind="event",
        source=CODEX_SOURCE,
        producer=CODEX_PRODUCER,
        service=CODEX_SERVICE,
        actor=_actor(payload),
        data=data,
        correlation_id=session.session_id,
        causation_id=session.last_event_id,
        ordering_key=ordering_key,
        event_id=event_id,
    )


def _shape_data(
    session: SessionState,
    hook_name: str,
    ce_type: str,
    payload: Any,
) -> dict[str, Any]:
    cwd = os.getcwd()
    raw = _raw(hook_name, payload)

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
            started = datetime.fromisoformat(session.started_at.replace("Z", "+00:00"))
            duration = int((datetime.now(started.tzinfo) - started).total_seconds())
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
        prompt_text = _value(payload, "prompt", "user_prompt", "userPrompt") or ""
        prompt_text = str(prompt_text)
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
            "thread_id": _value(payload, "thread_id", "threadId") or session.session_id,
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


def _publish(envelope: dict) -> bool:
    if os.environ.get("BLOODBANK_ENABLED", "true") != "true":
        return False
    subject = envelope["subject"]
    body = json.dumps(envelope).encode("utf-8")
    try:
        nats_publish(subject, body, client_name="codex-hooks-bridge")
    except (OSError, RuntimeError) as exc:
        _log(f"publish failed ({subject}): {exc}")
        if os.environ.get("BLOODBANK_HOOK_STRICT") == "1":
            raise
        return False
    if os.environ.get("BLOODBANK_HOOK_VERBOSE"):
        print(f"[bloodbank-codex-hook] published {subject}", file=sys.stderr)
    return True


def main(argv: list[str]) -> int:
    payload = _read_payload(argv)
    hook_name = _event_name(argv, payload)
    if not hook_name:
        print("usage: publish.py <hookName> [payload-json|end-reason]", file=sys.stderr)
        return 2

    mapping = HOOK_MAP.get(hook_name)
    if mapping is None:
        _log(f"unsupported hook name (ignored): {hook_name}")
        return 1 if os.environ.get("BLOODBANK_HOOK_STRICT") == "1" else 0

    ce_type, bucket_prefix = mapping
    session = SessionState(path=SESSION_FILE)
    if ce_type == "bloodbank.v1.agent.session.started":
        session.reset()

    try:
        data = _shape_data(session, hook_name, ce_type, payload)
        event_id = (
            session.session_id
            if ce_type == "bloodbank.v1.agent.session.started"
            else None
        )
        envelope = _build_event(
            session,
            ce_type,
            bucket_prefix,
            data,
            payload,
            event_id=event_id,
        )
        published = _publish(envelope)
    except Exception as exc:
        _log(f"handler failed hook={hook_name} type={ce_type} err={exc!r}")
        return 1 if os.environ.get("BLOODBANK_HOOK_STRICT") == "1" else 0

    if published:
        session.record_event(envelope["id"])
        if ce_type == "bloodbank.v1.agent.tool.completed":
            session.bump_tool(_tool_name(payload))
        if ce_type == "bloodbank.v1.agent.session.ended":
            session.archive(SESSIONS_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
