#!/usr/bin/env python3
"""Claude Code events recorder — subscribes to agent.* events via Dapr.

Bookend service for the `claude-events` compose profile. The publisher
runs on the host (`.claude/hooks/bloodbank-publisher.sh` in the
metarepo) and POSTs through a sibling daprd sidecar. This service
subscribes via Dapr to all three agent.* events and records them
in-memory for inspection.

Endpoints:
  GET  /dapr/subscribe        Dapr subscription list (3 routes)
  POST /events/session_started  Dapr delivers agent.session.started here
  POST /events/session_ended    Dapr delivers agent.session.ended here
  POST /events/tool_invoked     Dapr delivers agent.tool.invoked here
  GET  /inspect/recorded      test hook: count_by_type + sessions + envelopes
  POST /inspect/reset         test hook: clear recorded buffer
  GET  /healthz               liveness probe

Schema source of truth (post-V3-110):
  holyfields/schemas/agent/{session.started,session.ended,tool.invoked}.v1.json

Configuration:
  APP_PORT             HTTP port (default: 3001)
  SUBSCRIBE_PUBSUB     Dapr pubsub component (default: bloodbank-v3-pubsub)
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
SUBSCRIBE_PUBSUB = os.environ.get("SUBSCRIBE_PUBSUB", "bloodbank-v3-pubsub")
MAX_BUFFER = int(os.environ.get("MAX_BUFFER", "1024"))

# Topic + route mapping. Mirrors the publisher's topic choices in
# .claude/hooks/bloodbank-publisher.sh. Keep in sync if either side moves.
SUBSCRIPTIONS: list[dict] = [
    {
        "pubsubname": SUBSCRIBE_PUBSUB,
        "topic": "event.agent.session.started",
        "route": "/events/session_started",
    },
    {
        "pubsubname": SUBSCRIBE_PUBSUB,
        "topic": "event.agent.session.ended",
        "route": "/events/session_ended",
    },
    {
        "pubsubname": SUBSCRIBE_PUBSUB,
        "topic": "event.agent.tool.invoked",
        "route": "/events/tool_invoked",
    },
    {
        "pubsubname": SUBSCRIBE_PUBSUB,
        "topic": "event.agent.prompt.submitted",
        "route": "/events/prompt_submitted",
    },
    {
        "pubsubname": SUBSCRIBE_PUBSUB,
        "topic": "event.agent.tool.requested",
        "route": "/events/tool_requested",
    },
    {
        "pubsubname": SUBSCRIBE_PUBSUB,
        "topic": "event.agent.subagent.completed",
        "route": "/events/subagent_completed",
    },
]
ROUTE_TO_TYPE: dict[str, str] = {
    "/events/session_started": "agent.session.started",
    "/events/session_ended": "agent.session.ended",
    "/events/tool_invoked": "agent.tool.invoked",
    "/events/prompt_submitted": "agent.prompt.submitted",
    "/events/tool_requested": "agent.tool.requested",
    "/events/subagent_completed": "agent.subagent.completed",
}

_lock = threading.Lock()
_received: Deque[dict] = deque(maxlen=MAX_BUFFER)
_count_by_type: dict[str, int] = defaultdict(int)
# Per-session aggregate so tests can assert lifecycle without scanning the
# full buffer. Indexed by data.session_id.
_sessions: dict[str, dict] = {}


def _record(envelope: dict) -> None:
    with _lock:
        _received.append(envelope)
        ev_type = envelope.get("type", "unknown")
        _count_by_type[ev_type] += 1

        data = envelope.get("data", {}) if isinstance(envelope, dict) else {}
        sid = data.get("session_id")
        if not isinstance(sid, str):
            return

        entry = _sessions.get(sid) or {
            "session_id": sid,
            "started": False,
            "ended": False,
            "tool_invocations": 0,
            "tool_requests": 0,
            "prompts_submitted": 0,
            "subagents_completed": 0,
            "first_seen_type": ev_type,
        }
        # Track event time so test runlists can sort by recency.
        env_time = envelope.get("time")
        if isinstance(env_time, str):
            entry["last_seen"] = env_time

        if ev_type == "agent.session.started":
            entry["started"] = True
            entry["working_directory"] = data.get("working_directory")
            entry["git_branch"] = data.get("git_branch")
        elif ev_type == "agent.session.ended":
            entry["ended"] = True
            entry["end_reason"] = data.get("end_reason")
            entry["duration_seconds"] = data.get("duration_seconds")
            entry["total_turns"] = data.get("total_turns")
        elif ev_type == "agent.tool.invoked":
            entry["tool_invocations"] += 1
        elif ev_type == "agent.tool.requested":
            entry["tool_requests"] += 1
        elif ev_type == "agent.prompt.submitted":
            entry["prompts_submitted"] += 1
        elif ev_type == "agent.subagent.completed":
            entry["subagents_completed"] += 1
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
            data = envelope.get("data") or {}
            print(
                f"claude-events-recorder: recorded type={envelope.get('type')} "
                f"session_id={data.get('session_id')} "
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
