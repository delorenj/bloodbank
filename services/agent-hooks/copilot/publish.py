#!/usr/bin/env python3
"""GitHub Copilot CLI → Bloodbank hook publisher (v1 contract).

Reads a Copilot hook payload from stdin, builds a CloudEvents 1.0 envelope
per bloodbank/docs/event-naming.md, and publishes to NATS at
``bloodbank.evt.v1.<domain>.<entity>.<action>``.

Reference: https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-hooks

Usage (from ~/.copilot/hooks/bloodbank.json):
    python3 .../agent-hooks/copilot/publish.py <hookName>

Hook → v1 CloudEvents type mapping:
    sessionStart        → bloodbank.v1.cli.session.started
    sessionEnd          → bloodbank.v1.cli.session.ended
    userPromptSubmitted → bloodbank.v1.conversation.turn.started
    preToolUse          → bloodbank.v1.agent.tool.requested
    postToolUse         → bloodbank.v1.agent.tool.completed
    errorOccurred       → bloodbank.v1.agent.invocation.failed
    agentStop           → bloodbank.v1.agent.invocation.completed

Unknown hook names are rejected (no auto-translation fallback) since
arbitrary names cannot satisfy the v1 contract regex.
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
from core.nats_publish import publish as nats_publish  # noqa: E402
from core.session import SessionState  # noqa: E402

COPILOT_SOURCE = "urn:33god:integration:copilot-cli"
COPILOT_PRODUCER = "copilot-cli"
COPILOT_SERVICE = "copilot-hooks"
COPILOT_ACTOR: dict[str, Any] = {
    "type": "agent_cli",
    "agent_id": "bloodbank.agent.copilot",
    "cli": "copilot",
    "provider": "github_copilot",
    "model": None,
}

# Copilot camelCase hook → (v1 CloudEvents type, ordering bucket prefix)
HOOK_MAP: dict[str, tuple[str, str]] = {
    "sessionStart": ("bloodbank.v1.cli.session.started", "cli_session"),
    "sessionEnd": ("bloodbank.v1.cli.session.ended", "cli_session"),
    "userPromptSubmitted": ("bloodbank.v1.conversation.turn.started", "thread"),
    "preToolUse": ("bloodbank.v1.agent.tool.requested", "invocation"),
    "postToolUse": ("bloodbank.v1.agent.tool.completed", "invocation"),
    "errorOccurred": ("bloodbank.v1.agent.invocation.failed", "invocation"),
    "agentStop": ("bloodbank.v1.agent.invocation.completed", "invocation"),
}


def _session_path() -> Path:
    return Path.home() / ".copilot" / "bloodbank-session.json"


def _read_stdin() -> Any:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _log(msg: str) -> None:
    if os.environ.get("BLOODBANK_DEBUG") == "true" or os.environ.get(
        "BLOODBANK_HOOK_VERBOSE"
    ):
        print(f"[bloodbank-copilot-hook] {msg}", file=sys.stderr)


def _tool_call_id(session_id: str, hook_name: str, payload: Any) -> str:
    """Deterministic tool_call_id correlating pre/post pairs for the same tool use.

    Falls back to a payload-id when available; otherwise hashes (session, hook, frozen payload).
    """
    if isinstance(payload, dict):
        for key in ("tool_call_id", "toolUseId", "id"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                return v
    seed = json.dumps([session_id, hook_name, payload], sort_keys=True, default=str)
    return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:32]


def _shape_data(
    ce_type: str, session_id: str, hook_name: str, payload: Any
) -> dict[str, Any]:
    """Project the raw Copilot hook payload into the v1 schema's required data shape.

    Unknown keys from the hook payload are preserved under ``raw`` so downstream
    consumers can still inspect them.
    """
    raw = {"hook": hook_name, "payload": payload}

    if ce_type == "bloodbank.v1.cli.session.started":
        return {"session_id": session_id, **raw}
    if ce_type == "bloodbank.v1.cli.session.ended":
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
    # Defensive fallback (should be unreachable given HOOK_MAP).
    return raw


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].strip():
        print("usage: publish.py <hookName>", file=sys.stderr)
        return 2
    hook_name = argv[1].strip()

    mapping = HOOK_MAP.get(hook_name)
    if mapping is None:
        _log(f"unknown hook name (rejected): {hook_name}")
        # Hooks must never fail the agent; exit clean unless strict mode.
        return 1 if os.environ.get("BLOODBANK_HOOK_STRICT") == "1" else 0

    ce_type, bucket_prefix = mapping
    payload = _read_stdin()
    session = SessionState(path=_session_path())

    # Fresh session id on each new Copilot session keeps causation chains
    # bounded to one CLI run.
    if hook_name == "sessionStart":
        session.reset()

    ordering_key = f"{bucket_prefix}:{session.session_id}"
    data = _shape_data(ce_type, session.session_id, hook_name, payload)
    event_id = session.session_id if hook_name == "sessionStart" else None

    try:
        envelope = build_envelope(
            ce_type=ce_type,
            kind="event",
            source=COPILOT_SOURCE,
            producer=COPILOT_PRODUCER,
            service=COPILOT_SERVICE,
            actor=COPILOT_ACTOR,
            data=data,
            correlation_id=session.session_id,
            causation_id=session.last_event_id,
            ordering_key=ordering_key,
            event_id=event_id,
        )
    except Exception as exc:  # contract violation — log and exit clean
        _log(f"envelope build failed type={ce_type} err={exc!r}")
        return 1 if os.environ.get("BLOODBANK_HOOK_STRICT") == "1" else 0

    subject = envelope["subject"]
    body = json.dumps(envelope).encode("utf-8")

    if os.environ.get("BLOODBANK_ENABLED", "true") != "true":
        return 0

    try:
        nats_publish(subject, body, client_name="copilot-hooks-bridge")
    except (OSError, RuntimeError) as exc:
        _log(f"publish failed ({subject}): {exc}")
        if os.environ.get("BLOODBANK_HOOK_STRICT") == "1":
            return 1
        return 0

    session.record_event(envelope["id"])
    _log(f"published {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
