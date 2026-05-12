#!/usr/bin/env python3
"""Publish a GitHub Copilot CLI hook event to Bloodbank NATS.

Standalone, stdlib-only. Invoked by entries in `~/.copilot/hooks/bloodbank.json`.

Usage:
    copilot_hook_publish.py <hook-name>
    # Hook payload JSON is read from stdin.

The script:
  1. Reads the hook name from argv[1] (e.g. "sessionStart").
  2. Reads the Copilot hook payload from stdin (JSON; may be empty).
  3. Builds a CloudEvents 1.0 envelope.
  4. Publishes it to NATS at subject ``event.copilot.<dotted-hook-name>``.

The 33GOD `bloodbank-event-toaster` catch-all consumer subscribes to ``event.>``
and forwards every envelope to https://ntfy.delo.sh/bloodbank, which is how
this script is verified end-to-end.

Why raw NATS protocol? The bloodbank Python service does not depend on
`nats-py`, and we want this script to be drop-in usable without a virtualenv
or any third-party install. NATS' wire protocol is text-based and the PUB
verb is trivial, so a ~40-line socket implementation is fine for one-shot
fire-and-forget publishes from a CLI hook.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid
from datetime import datetime, timezone

NATS_HOST = os.environ.get("BLOODBANK_NATS_HOST", "127.0.0.1")
NATS_PORT = int(os.environ.get("BLOODBANK_NATS_PORT", "4222"))
NATS_TIMEOUT = float(os.environ.get("BLOODBANK_NATS_TIMEOUT", "3.0"))

# camelCase Copilot hook names → dotted event suffix.
# Anything outside this map is allowed and gets a best-effort camelCase split.
HOOK_SUBJECT_MAP: dict[str, str] = {
    "sessionStart":        "session.started",
    "sessionEnd":          "session.ended",
    "userPromptSubmitted": "prompt.submitted",
    "preToolUse":          "tool.pre",
    "postToolUse":         "tool.post",
    "errorOccurred":       "error.occurred",
    "agentStop":           "agent.stopped",
}


def _camel_to_dot(name: str) -> str:
    """Fallback: 'someHookName' -> 'some.hook.name'."""
    out: list[str] = []
    for ch in name:
        if ch.isupper() and out:
            out.append(".")
        out.append(ch.lower())
    return "".join(out)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_envelope(hook_name: str, event_subject: str, payload: object) -> dict:
    """CloudEvents 1.0 envelope. Matches the shape the event-toaster expects."""
    return {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": "urn:33god:integration:copilot-cli",
        "type": f"copilot.{event_subject}",
        "subject": f"copilot/{hook_name}",
        "time": _now_iso(),
        "datacontenttype": "application/json",
        "producer": "copilot-cli",
        "service": "copilot-hooks",
        "domain": "copilot",
        "data": {
            "hook": hook_name,
            "payload": payload,
        },
    }


def publish_nats(subject: str, body: bytes) -> None:
    """Open one TCP connection to NATS, PUB, drain with PING/PONG, close."""
    with socket.create_connection((NATS_HOST, NATS_PORT), timeout=NATS_TIMEOUT) as sock:
        sock.settimeout(NATS_TIMEOUT)
        f = sock.makefile("rwb", buffering=0)

        # Server greets with an INFO line we don't need to parse.
        f.readline()

        connect_opts = {
            "verbose": False,
            "pedantic": False,
            "tls_required": False,
            "name": "copilot-hooks-bridge",
            "lang": "python-stdlib",
            "version": "1.0",
            "protocol": 1,
        }
        f.write(b"CONNECT " + json.dumps(connect_opts).encode("utf-8") + b"\r\n")
        f.write(b"PUB " + subject.encode("ascii") + b" "
                + str(len(body)).encode("ascii") + b"\r\n" + body + b"\r\n")
        # PING/PONG round-trip flushes the PUB before we close.
        f.write(b"PING\r\n")
        f.flush()

        deadline = time.monotonic() + NATS_TIMEOUT
        while time.monotonic() < deadline:
            line = f.readline()
            if not line:
                break
            if line.startswith(b"PONG"):
                return
            if line.startswith(b"-ERR"):
                raise RuntimeError(f"NATS server rejected publish: {line!r}")
            # Ignore stray +OK, INFO, PING from server.


def read_stdin_payload() -> object:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: copilot_hook_publish.py <hookName>", file=sys.stderr)
        return 2

    hook_name = argv[1].strip()
    if not hook_name:
        print("error: empty hook name", file=sys.stderr)
        return 2

    event_subject = HOOK_SUBJECT_MAP.get(hook_name) or _camel_to_dot(hook_name)
    nats_subject = f"event.copilot.{event_subject}"

    payload = read_stdin_payload()
    envelope = build_envelope(hook_name, event_subject, payload)
    body = json.dumps(envelope).encode("utf-8")

    try:
        publish_nats(nats_subject, body)
    except (OSError, RuntimeError) as exc:
        # Hooks must never fail the agent — log to stderr and exit 0 by default.
        print(f"copilot-hook publish failed ({nats_subject}): {exc}", file=sys.stderr)
        if os.environ.get("BLOODBANK_HOOK_STRICT") == "1":
            return 1
        return 0

    if os.environ.get("BLOODBANK_HOOK_VERBOSE"):
        print(f"copilot-hook published {nats_subject}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
