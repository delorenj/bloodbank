#!/usr/bin/env python3
"""Heartbeat tick producer — first real-world v3 event publisher.

Long-running service that emits `system.heartbeat.tick` events through
Dapr pub/sub on a configurable interval. Each tick carries a monotonic
sequence number, an instance-stable producer_id, and the producer's
start time so consumers can detect restarts.

Schema: holyfields/schemas/system/heartbeat.tick.v1.json (extends
cloudevent_base.v1.json). This service constructs the envelope as a
JSON dict directly; switching to the Holyfields-generated Pydantic
model is a follow-up once the holyfields installable-package story is
stable inside containers.

Configuration via env vars:
  DAPR_HTTP_HOST       Hostname of the daprd sidecar (default: daprd-heartbeat)
  DAPR_HTTP_PORT       Port of the daprd HTTP API (default: 3500)
  DAPR_PUBSUB          Dapr pubsub component name (default: bloodbank-v3-pubsub)
  HEARTBEAT_INTERVAL   Tick interval in seconds (default: 5)
  HEARTBEAT_TOPIC      Dapr topic / NATS subject (default: event.system.heartbeat.tick)
  PRODUCER_ID          Stable per-instance id (default: heartbeat-tick:<random>)
  LOG_LEVEL            INFO / DEBUG (default: INFO)

Stdlib only (urllib + json) so the container image stays minimal.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib import error as urllib_error
from urllib import request as urllib_request

LOG = logging.getLogger("heartbeat-tick")


def _now_iso() -> str:
    """RFC3339 UTC timestamp matching the heartbeat schema's `time` field."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_envelope(*, tick_seq: int, producer_id: str, started_at: str, interval_ms: int) -> dict:
    """Construct a CloudEvents 1.0 envelope matching system.heartbeat.tick.v1."""
    return {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": "urn:33god:service:heartbeat-tick",
        "type": "system.heartbeat.tick",
        "subject": f"system/{producer_id}",
        "time": _now_iso(),
        "datacontenttype": "application/json",
        "dataschema": "apicurio://holyfields/system.heartbeat.tick/versions/1",
        "correlationid": str(uuid.uuid4()),
        "causationid": None,
        "producer": "heartbeat-tick",
        "service": "heartbeat-tick",
        "domain": "system",
        "schemaref": "system.heartbeat.tick.v1",
        "traceparent": "00-00000000000000000000000000000000-0000000000000000-00",
        "data": {
            "tick_seq": tick_seq,
            "interval_ms": interval_ms,
            "producer_id": producer_id,
            "started_at": started_at,
        },
    }


def publish(dapr_url: str, pubsub: str, topic: str, envelope: dict, timeout: float = 5.0) -> None:
    """POST the envelope to Dapr pub/sub. Raises on non-2xx."""
    url = f"{dapr_url}/v1.0/publish/{pubsub}/{topic}"
    body = json.dumps(envelope).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/cloudevents+json"},
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"dapr publish returned HTTP {resp.status}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


_running = True


def _handle_signal(signum: int, _frame: object) -> None:
    global _running
    LOG.info("received signal %d; draining and exiting", signum)
    _running = False


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    dapr_host = os.environ.get("DAPR_HTTP_HOST", "daprd-heartbeat")
    dapr_port = os.environ.get("DAPR_HTTP_PORT", "3500")
    dapr_url = f"http://{dapr_host}:{dapr_port}"
    pubsub = os.environ.get("DAPR_PUBSUB", "bloodbank-v3-pubsub")
    topic = os.environ.get("HEARTBEAT_TOPIC", "event.system.heartbeat.tick")
    interval_s = float(os.environ.get("HEARTBEAT_INTERVAL", "5"))
    interval_ms = int(interval_s * 1000)

    producer_id = os.environ.get(
        "PRODUCER_ID", f"heartbeat-tick:{uuid.uuid4().hex[:8]}"
    )
    started_at = _now_iso()

    LOG.info(
        "starting heartbeat-tick: dapr=%s pubsub=%s topic=%s interval=%.2fs producer_id=%s",
        dapr_url, pubsub, topic, interval_s, producer_id,
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Wait for daprd to come up before first publish. Boot races are real;
    # we tolerate up to 60s of "connection refused" before giving up.
    deadline = time.monotonic() + 60.0
    while _running and time.monotonic() < deadline:
        try:
            with urllib_request.urlopen(f"{dapr_url}/v1.0/healthz", timeout=2.0) as resp:
                if resp.status in (200, 204):
                    LOG.info("daprd healthy")
                    break
        except (urllib_error.URLError, ConnectionError, TimeoutError) as exc:
            LOG.debug("daprd not yet ready: %s", exc)
        time.sleep(2)
    else:
        if not _running:
            return 0
        LOG.error("daprd never became healthy at %s", dapr_url)
        return 1

    tick_seq = 0
    while _running:
        envelope = build_envelope(
            tick_seq=tick_seq,
            producer_id=producer_id,
            started_at=started_at,
            interval_ms=interval_ms,
        )
        try:
            publish(dapr_url, pubsub, topic, envelope)
            LOG.info("emitted tick_seq=%d id=%s", tick_seq, envelope["id"])
            tick_seq += 1
        except Exception as exc:
            # Don't crash the loop on transient publish failures; let CI
            # / observability surface it through tick_seq stalling.
            LOG.warning("publish failed (tick_seq=%d): %s", tick_seq, exc)

        # Sleep in small chunks so SIGTERM is responsive (no 5-30s delay
        # to drain on shutdown).
        slept = 0.0
        while _running and slept < interval_s:
            chunk = min(0.5, interval_s - slept)
            time.sleep(chunk)
            slept += chunk

    LOG.info("heartbeat-tick exiting; final tick_seq=%d", tick_seq - 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
