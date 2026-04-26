#!/usr/bin/env python3
"""Heartbeat tick recorder — subscribes via Dapr and records each tick.

Bookend to heartbeat-tick. Implements the Dapr programmatic subscription
contract (mirrors ops/v3/smoketest/echo-sub/app.py) but with stronger
typing intent: it counts ticks, captures the latest envelope per
producer_id, and exposes test hooks.

Endpoints:
  GET  /dapr/subscribe       Dapr subscription list
  POST /events/heartbeat     Dapr delivers heartbeat ticks here
  GET  /inspect/recorded     test hook: count, latest, producer_summary
  POST /inspect/reset        test hook: clear recorded buffer
  GET  /healthz              liveness probe

Schema source of truth: holyfields/schemas/system/heartbeat.tick.v1.json

Configuration:
  APP_PORT             HTTP port (default: 3001)
  SUBSCRIBE_PUBSUB     Dapr pubsub component (default: bloodbank-v3-pubsub)
  SUBSCRIBE_TOPIC      Subscription topic (default: event.system.heartbeat.tick)
  SUBSCRIBE_ROUTE      Delivery route (default: /events/heartbeat)
  MAX_BUFFER           Max recorded envelopes (default: 1024; FIFO eviction)

Stdlib only.
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
SUBSCRIBE_TOPIC = os.environ.get("SUBSCRIBE_TOPIC", "event.system.heartbeat.tick")
SUBSCRIBE_ROUTE = os.environ.get("SUBSCRIBE_ROUTE", "/events/heartbeat")
MAX_BUFFER = int(os.environ.get("MAX_BUFFER", "1024"))

_lock = threading.Lock()
_received: Deque[dict] = deque(maxlen=MAX_BUFFER)
# Per-producer summary so tests can verify monotonic tick_seq without
# walking the entire buffer.
_producer_summary: dict[str, dict] = {}


def _subscribe_response() -> list[dict]:
    return [
        {
            "pubsubname": SUBSCRIBE_PUBSUB,
            "topic": SUBSCRIBE_TOPIC,
            "route": SUBSCRIBE_ROUTE,
        }
    ]


def _record(envelope: dict) -> None:
    """Append an envelope to the buffer and update per-producer summary."""
    with _lock:
        _received.append(envelope)
        data = envelope.get("data", {}) if isinstance(envelope, dict) else {}
        producer_id = data.get("producer_id")
        tick_seq = data.get("tick_seq")
        if isinstance(producer_id, str) and isinstance(tick_seq, int):
            entry = _producer_summary.get(producer_id) or {
                "producer_id": producer_id,
                "first_tick_seq": tick_seq,
                "last_tick_seq": tick_seq,
                "count": 0,
                "started_at": data.get("started_at"),
            }
            entry["last_tick_seq"] = max(entry["last_tick_seq"], tick_seq)
            entry["first_tick_seq"] = min(entry["first_tick_seq"], tick_seq)
            entry["count"] += 1
            _producer_summary[producer_id] = entry


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
            self._send_json(200, _subscribe_response())
            return

        if self.path == "/inspect/recorded":
            with _lock:
                envelopes = list(_received)
                summary = list(_producer_summary.values())
            latest = envelopes[-1] if envelopes else None
            self._send_json(
                200,
                {
                    "count": len(envelopes),
                    "latest": latest,
                    "producers": summary,
                    "envelopes": envelopes,
                },
            )
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
                f"heartbeat-recorder: recorded "
                f"tick_seq={envelope.get('data', {}).get('tick_seq')} "
                f"producer_id={envelope.get('data', {}).get('producer_id')} "
                f"buffer={buffer_size}/{MAX_BUFFER}",
                file=sys.stdout,
                flush=True,
            )
            # Dapr expects a status acknowledgement.
            self._send_json(200, {"status": "SUCCESS"})
            return

        if self.path == "/inspect/reset":
            with _lock:
                cleared = len(_received)
                _received.clear()
                _producer_summary.clear()
            self._send_json(200, {"cleared": cleared})
            return

        self._send_empty(404)


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", APP_PORT), Handler)
    print(
        f"heartbeat-recorder: listening on 0.0.0.0:{APP_PORT} | "
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
