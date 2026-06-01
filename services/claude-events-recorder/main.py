#!/usr/bin/env python3
"""Claude Code events recorder — subscribes to Bloodbank v1 agent CLI events.

Bookend service for the `claude-events` compose profile. The publisher
runs on the host (`bloodbank/services/agent-hooks/claude/publish.py`,
invoked from `.claude/settings.json`) and PUBs directly to NATS. This
service subscribes via Dapr to the v1 events the hook emits and records
them in-memory for inspection.

Subscriptions (v1 contract topics per docs/event-naming.md §15):
  bloodbank.evt.v1.cli.session.started
  bloodbank.evt.v1.cli.session.ended
  bloodbank.evt.v1.conversation.turn.started
  bloodbank.evt.v1.agent.tool.requested
  bloodbank.evt.v1.agent.tool.invoked
  bloodbank.evt.v1.agent.invocation.completed

Endpoints:
  GET  /dapr/subscribe          Dapr subscription list
  POST /events/<route>          Dapr delivers events here
  GET  /inspect/recorded        test hook: count_by_type + sessions + envelopes
  POST /inspect/reset           test hook: clear recorded buffer
  GET  /healthz                 liveness probe

Schema source of truth: bloodbank/schemas/bloodbank/v1/<domain>/<entity>.<action>.v1.json

Configuration:
  APP_PORT             HTTP port (default: 3001)
  SUBSCRIBE_PUBSUB     Dapr pubsub component (default: bloodbank-pubsub)
  MAX_BUFFER           Max recorded envelopes (default: 1024; FIFO eviction)

Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Deque

APP_PORT = int(os.environ.get("APP_PORT", "3001"))
SUBSCRIBE_PUBSUB = os.environ.get("SUBSCRIBE_PUBSUB", "bloodbank-pubsub")
MAX_BUFFER = int(os.environ.get("MAX_BUFFER", "1024"))

# (route_path, dapr_topic, v1_ce_type) tuples — keep in sync with the
# publishers in services/agent-hooks/claude/publish.py and copilot/publish.py.
ROUTES: list[tuple[str, str, str]] = [
    ("/events/cli_session_started",   "bloodbank.evt.v1.cli.session.started",        "bloodbank.v1.cli.session.started"),
    ("/events/cli_session_ended",     "bloodbank.evt.v1.cli.session.ended",          "bloodbank.v1.cli.session.ended"),
    ("/events/turn_started",          "bloodbank.evt.v1.conversation.turn.started",  "bloodbank.v1.conversation.turn.started"),
    ("/events/tool_call_requested",   "bloodbank.evt.v1.agent.tool.requested",   "bloodbank.v1.agent.tool.requested"),
    ("/events/tool_call_invoked",     "bloodbank.evt.v1.agent.tool.invoked",     "bloodbank.v1.agent.tool.invoked"),
    ("/events/tool_call_completed",   "bloodbank.evt.v1.agent.tool.completed",   "bloodbank.v1.agent.tool.completed"),
    ("/events/agent_invocation_completed", "bloodbank.evt.v1.agent.invocation.completed", "bloodbank.v1.agent.invocation.completed"),
    ("/events/agent_invocation_failed",    "bloodbank.evt.v1.agent.invocation.failed",    "bloodbank.v1.agent.invocation.failed"),
]
SUBSCRIPTIONS: list[dict] = [
    {"pubsubname": SUBSCRIBE_PUBSUB, "topic": topic, "route": route}
    for (route, topic, _) in ROUTES
]
ROUTE_TO_TYPE: dict[str, str] = {route: ce_type for (route, _, ce_type) in ROUTES}

_lock = threading.Lock()
_received: Deque[dict] = deque(maxlen=MAX_BUFFER)
_count_by_type: dict[str, int] = defaultdict(int)
# Per-session aggregate so tests can assert lifecycle without scanning the
# full buffer. Keyed by envelope.correlationid (the canonical session key
# under the v1 contract — the publishers set correlationid = session_id).
_sessions: dict[str, dict] = {}


def _record(envelope: dict) -> None:
    with _lock:
        _received.append(envelope)
        ev_type = envelope.get("type", "unknown")
        _count_by_type[ev_type] += 1

        # Session correlation: prefer correlationid, fall back to data IDs.
        sid = envelope.get("correlationid")
        if not isinstance(sid, str):
            data = envelope.get("data", {}) if isinstance(envelope, dict) else {}
            sid = data.get("session_id") or data.get("thread_id") or data.get("invocation_id")
        if not isinstance(sid, str):
            return

        data = envelope.get("data", {}) if isinstance(envelope, dict) else {}
        entry = _sessions.get(sid) or {
            "session_id": sid,
            "started": False,
            "ended": False,
            "tool_invocations": 0,
            "tool_requests": 0,
            "tool_completions": 0,
            "prompts_submitted": 0,
            "subagents_completed": 0,
            "subagents_failed": 0,
            "first_seen_type": ev_type,
        }
        env_time = envelope.get("time")
        if isinstance(env_time, str):
            entry["last_seen"] = env_time
        actor = envelope.get("actor") or {}
        if isinstance(actor, dict):
            entry["cli"] = actor.get("cli") or entry.get("cli")
            entry["provider"] = actor.get("provider") or entry.get("provider")

        if ev_type == "bloodbank.v1.cli.session.started":
            entry["started"] = True
            entry["working_directory"] = data.get("working_directory")
            entry["git_branch"] = data.get("git_branch")
        elif ev_type == "bloodbank.v1.cli.session.ended":
            entry["ended"] = True
            entry["end_reason"] = data.get("end_reason")
            entry["duration_seconds"] = data.get("duration_seconds")
            entry["total_turns"] = data.get("total_turns")
        elif ev_type == "bloodbank.v1.agent.tool.requested":
            entry["tool_requests"] += 1
        elif ev_type == "bloodbank.v1.agent.tool.invoked":
            entry["tool_invocations"] += 1
        elif ev_type == "bloodbank.v1.agent.tool.completed":
            entry["tool_completions"] += 1
        elif ev_type == "bloodbank.v1.conversation.turn.started":
            entry["prompts_submitted"] += 1
        elif ev_type == "bloodbank.v1.agent.invocation.completed":
            entry["subagents_completed"] += 1
        elif ev_type == "bloodbank.v1.agent.invocation.failed":
            entry["subagents_failed"] += 1
        _sessions[sid] = entry


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return  # silence default per-request log

    def _send_json(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/dapr/subscribe":
            self._send_json(200, SUBSCRIPTIONS)
            return

        if self.path == "/inspect/recorded":
            with _lock:
                envelopes = list(_received)
                count_by_type = dict(_count_by_type)
                sessions = list(_sessions.values())
            self._send_json(
                200,
                {
                    "count": len(envelopes),
                    "count_by_type": count_by_type,
                    "sessions": sessions,
                    "envelopes": envelopes,
                },
            )
            return

        if self.path == "/healthz":
            self._send_empty(204)
            return

        self._send_empty(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path in ROUTE_TO_TYPE:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                envelope = json.loads(raw.decode("utf-8")) if raw else None
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_empty(400)
                return

            if not isinstance(envelope, dict):
                self._send_empty(400)
                return

            _record(envelope)

            with _lock:
                buffer_size = len(_received)
            print(
                f"claude-events-recorder: recorded type={envelope.get('type')} "
                f"correlationid={envelope.get('correlationid')} "
                f"buffer={buffer_size}/{MAX_BUFFER}",
                file=sys.stdout,
                flush=True,
            )
            self._send_json(200, {"status": "SUCCESS"})
            return

        if self.path == "/inspect/reset":
            with _lock:
                cleared = len(_received)
                _received.clear()
                _count_by_type.clear()
                _sessions.clear()
            self._send_json(200, {"cleared": cleared})
            return

        self._send_empty(404)


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", APP_PORT), Handler)
    routes = ", ".join(f"{s['topic']}->{s['route']}" for s in SUBSCRIPTIONS)
    print(
        f"claude-events-recorder: listening on 0.0.0.0:{APP_PORT} | "
        f"subscribe {SUBSCRIBE_PUBSUB} {routes}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
