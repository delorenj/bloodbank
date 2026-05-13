#!/usr/bin/env python3
"""GitHub Copilot CLI → Bloodbank hook publisher.

Reads a Copilot hook payload from stdin, builds a CloudEvents 1.0
envelope (correlationid + causationid linked via SessionState), and
publishes to NATS at ``event.copilot.<*>``.

Reference: https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-hooks

Usage (from ~/.copilot/hooks/bloodbank.json):
    python3 .../agent-hooks/copilot/publish.py <hookName>
"""
from __future__ import annotations

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
COPILOT_DOMAIN = "copilot"

# camelCase Copilot hook → (CloudEvents type, NATS subject suffix).
# Unknown hooks fall through a camelCase → dotted transform.
HOOK_MAP: dict[str, tuple[str, str]] = {
    "sessionStart":        ("copilot.session.started",  "session.started"),
    "sessionEnd":          ("copilot.session.ended",    "session.ended"),
    "userPromptSubmitted": ("copilot.prompt.submitted", "prompt.submitted"),
    "preToolUse":          ("copilot.tool.pre",         "tool.pre"),
    "postToolUse":         ("copilot.tool.post",        "tool.post"),
    "errorOccurred":       ("copilot.error.occurred",   "error.occurred"),
    "agentStop":           ("copilot.agent.stopped",    "agent.stopped"),
}


def _camel_to_dot(name: str) -> str:
    """'someHookName' -> 'some.hook.name'."""
    out: list[str] = []
    for ch in name:
        if ch.isupper() and out:
            out.append(".")
        out.append(ch.lower())
    return "".join(out)


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
    if os.environ.get("BLOODBANK_DEBUG") == "true" or os.environ.get("BLOODBANK_HOOK_VERBOSE"):
        print(f"[bloodbank-copilot-hook] {msg}", file=sys.stderr)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].strip():
        print("usage: publish.py <hookName>", file=sys.stderr)
        return 2
    hook_name = argv[1].strip()
    ce_type, subject_suffix = HOOK_MAP.get(
        hook_name,
        (f"copilot.{_camel_to_dot(hook_name)}", _camel_to_dot(hook_name)),
    )

    payload = _read_stdin()
    session = SessionState(path=_session_path())

    # A fresh session id on each new Copilot session keeps causation chains
    # bounded to one CLI run.
    if hook_name == "sessionStart":
        session.reset()

    envelope = build_envelope(
        ce_type=ce_type,
        subject=f"copilot/{hook_name}",
        source=COPILOT_SOURCE,
        producer=COPILOT_PRODUCER,
        service=COPILOT_SERVICE,
        domain=COPILOT_DOMAIN,
        data={"hook": hook_name, "payload": payload},
        correlation_id=session.session_id,
        causation_id=session.last_event_id,
        # On a fresh sessionStart, make the event id == session id so the
        # chain self-roots cleanly.
        event_id=session.session_id if hook_name == "sessionStart" else None,
    )

    nats_subject = f"event.copilot.{subject_suffix}"
    body = json.dumps(envelope).encode("utf-8")

    if os.environ.get("BLOODBANK_ENABLED", "true") != "true":
        return 0

    try:
        nats_publish(nats_subject, body, client_name="copilot-hooks-bridge")
    except (OSError, RuntimeError) as exc:
        # Hooks must never fail the agent — log to stderr (debug only) and exit
        # 0 unless explicitly told to fail loudly.
        _log(f"publish failed ({nats_subject}): {exc}")
        if os.environ.get("BLOODBANK_HOOK_STRICT") == "1":
            return 1
        return 0

    session.record_event(envelope["id"])
    _log(f"published {nats_subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
