#!/usr/bin/env python3
"""Minimal Dapr subscriber app for the v3 Dapr subscribe smoke test.

Implements the Dapr programmatic subscription contract using only the
Python standard library (http.server) so the container needs no
third-party dependencies.

Endpoints:
  GET  /dapr/subscribe       -> returns the subscription list Dapr uses
                                to route deliveries back to this app.
  POST /events/smoketest     -> Dapr delivers matching messages here.
                                Stores them in memory for inspection.
  GET  /inspect/received     -> test hook: returns everything received
                                since process start (or /inspect/reset).
  POST /inspect/reset        -> test hook: clear the received-message
                                buffer; returns the count cleared.
  GET  /healthz              -> liveness probe (always 200 while process
                                is running).

Configuration via environment:
  APP_PORT             -- HTTP port to listen on (default: 3001)
  SUBSCRIBE_PUBSUB     -- Dapr pubsub component name (default:
                          bloodbank-v3-pubsub)
  SUBSCRIBE_TOPIC      -- Topic (and NATS subject) to subscribe to
                          (default: event.dapr.subscribe.ping)
  SUBSCRIBE_ROUTE      -- HTTP path Dapr POSTs messages to (default:
                          /events/smoketest)

Design notes:
  * Per-process in-memory buffer. This is a SMOKE TEST app, not a real
    subscriber. Buffer is cleared by process restart or /inspect/reset.
  * Bounded buffer (MAX_BUFFER) so a misbehaving publisher cannot OOM
    the container. Old messages are evicted FIFO.
  * Thread-safe: http.server uses a thread per request; access to the
    buffer is guarded by a lock.
  * No CloudEvents validation in-app — the smoke test script validates
    after retrieving from /inspect/received, mirroring the
    smoketest-dapr.sh pattern.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Deque

APP_PORT = int(os.environ.get("APP_PORT", "3001"))
SUBSCRIBE_PUBSUB = os.environ.get("SUBSCRIBE_PUBSUB", "bloodbank-v3-pubsub")
SUBSCRIBE_TOPIC = os.environ.get("SUBSCRIBE_TOPIC", "event.dapr.subscribe.ping")
SUBSCRIBE_ROUTE = os.environ.get("SUBSCRIBE_ROUTE", "/events/smoketest")

MAX_BUFFER = 1024

_lock = threading.Lock()
_received: Deque[dict] = deque(maxlen=MAX_BUFFER)


def _subscribe_response() -> list[dict]:
    """Return the subscription list Dapr queries at startup."""
    return [
        {
            "pubsubname": SUBSCRIBE_PUBSUB,
            "topic": SUBSCRIBE_TOPIC,
            "route": SUBSCRIBE_ROUTE,
        }
    ]


class Handler(BaseHTTPRequestHandler):
    # Silence the default per-request log line; we emit our own structured
    # lines so CI output stays readable.
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

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
            self._send_json(200, _subscribe_response())
            return

        if self.path == "/inspect/received":
            with _lock:
                snapshot = list(_received)
            self._send_json(200, {"count": len(snapshot), "messages": snapshot})
            return

        if self.path == "/healthz":
            self._send_empty(204)
            return

        self._send_empty(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == SUBSCRIBE_ROUTE:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                message = json.loads(raw.decode("utf-8")) if raw else None
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Dapr will retry per its delivery policy; return 400.
                self._send_empty(400)
                return

            with _lock:
                _received.append(message if isinstance(message, dict) else {"raw": raw.decode("utf-8", errors="replace")})

            print(
                f"echo-sub: received message on {SUBSCRIBE_ROUTE} "
                f"(buffer={len(_received)}/{MAX_BUFFER})",
                file=sys.stdout,
                flush=True,
            )
            # 200 with a Dapr-status ACK is the canonical ack.
            self._send_json(200, {"status": "SUCCESS"})
            return

        if self.path == "/inspect/reset":
            with _lock:
                cleared = len(_received)
                _received.clear()
            self._send_json(200, {"cleared": cleared})
            return

        self._send_empty(404)


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", APP_PORT), Handler)
    print(
        f"echo-sub: listening on 0.0.0.0:{APP_PORT} | "
        f"subscribe {SUBSCRIBE_PUBSUB}/{SUBSCRIBE_TOPIC} -> {SUBSCRIBE_ROUTE}",
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
