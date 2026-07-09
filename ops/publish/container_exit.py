#!/usr/bin/env python3
"""Publish docker container exit events into Bloodbank."""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

SERVICE_DIR = Path(__file__).resolve().parents[2] / "services" / "agent-hooks"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from core.envelope import build_envelope  # noqa: E402
from core.nats_publish import publish as nats_publish  # noqa: E402

CE_TYPE = os.environ.get(
    "BLOODBANK_CONTAINER_EXIT_CE_TYPE",
    "bloodbank.v1.system.process.exited",
)
SOURCE = os.environ.get(
    "BLOODBANK_CONTAINER_EXIT_SOURCE",
    "urn:33god:service:docker-health-monitor",
)
PRODUCER = os.environ.get("BLOODBANK_CONTAINER_EXIT_PRODUCER", "docker-health-monitor")
SERVICE = os.environ.get("BLOODBANK_CONTAINER_EXIT_SERVICE", "docker-health-monitor")


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("container-exit publisher: stdin payload required")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise SystemExit("container-exit publisher: payload must be a JSON object")
    return payload


def _stable_uuid(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _actor() -> dict[str, Any]:
    return {
        "type": "service",
        "agent_id": "service:docker-health-monitor",
        "cli": None,
        "provider": None,
        "model": None,
    }


def _ordering_key(payload: dict[str, Any]) -> str:
    project = payload.get("compose_project") or "unknown-project"
    service = payload.get("compose_service") or payload.get("container_name") or "unknown-service"
    return f"container:{project}:{service}"


def _incident_data(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": payload.get("container_name") or payload.get("container_id") or "unknown-container",
        "runtime": "docker",
        "resource_kind": "container",
        "container_name": payload.get("container_name"),
        "container_id": payload.get("container_id"),
        "image": payload.get("image"),
        "compose_project": payload.get("compose_project"),
        "compose_service": payload.get("compose_service"),
        "compose_file": payload.get("compose_file"),
        "working_dir": payload.get("working_dir"),
        "status": payload.get("status"),
        "exit_code": payload.get("exit_code"),
        "restart_policy": payload.get("restart_policy"),
        "finished_at": payload.get("finished_at"),
        "started_at": payload.get("started_at"),
        "observed_at": payload.get("observed_at"),
        "incident_summary": payload.get("incident_summary"),
        "expected_exit": payload.get("expected_exit", False),
        "recovery_expected": payload.get("recovery_expected", False),
        "fingerprint": payload.get("fingerprint"),
    }


def main() -> int:
    payload = _read_payload()
    fingerprint = payload.get("fingerprint") or "|".join(
        str(payload.get(key, "")) for key in ("container_name", "finished_at", "exit_code", "status")
    )
    root_id = _stable_uuid(f"bloodbank:container-exit:{fingerprint}")

    envelope = build_envelope(
        ce_type=CE_TYPE,
        kind="event",
        source=SOURCE,
        producer=PRODUCER,
        service=SERVICE,
        actor=_actor(),
        data=_incident_data(payload),
        correlation_id=root_id,
        causation_id=root_id,
        event_id=root_id,
        ordering_key=_ordering_key(payload),
    )
    subject = envelope["subject"]
    nats_publish(subject, json.dumps(envelope).encode("utf-8"), client_name="docker-health-monitor")
    print(json.dumps({"subject": subject, "id": envelope["id"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
