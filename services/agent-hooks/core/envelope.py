"""CloudEvents 1.0 envelope builder for Bloodbank agent hooks.

Envelope shape follows holyfields/schemas/_common/cloudevent_base.v1.json.
Per bloodbank/CLAUDE.md, every event MUST carry correlationid and
causationid — this builder enforces it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def new_uuid() -> str:
    return str(uuid.uuid4())


def build_envelope(
    *,
    ce_type: str,
    subject: str,
    source: str,
    producer: str,
    service: str,
    domain: str,
    data: Any,
    correlation_id: str,
    causation_id: str,
    event_id: str | None = None,
    schemaref: str | None = None,
) -> dict:
    """Build a CloudEvents 1.0 envelope.

    correlation_id and causation_id are mandatory. For the first event in a
    chain (e.g. a session-start) callers should pass causation_id == correlation_id
    so the chain self-roots cleanly.
    """
    if not correlation_id:
        raise ValueError("correlation_id is required")
    if not causation_id:
        raise ValueError("causation_id is required")
    return {
        "specversion": "1.0",
        "id": event_id or new_uuid(),
        "source": source,
        "type": ce_type,
        "subject": subject,
        "time": now_iso(),
        "datacontenttype": "application/json",
        "correlationid": correlation_id,
        "causationid": causation_id,
        "producer": producer,
        "service": service,
        "domain": domain,
        "schemaref": schemaref or f"{ce_type}.v1",
        "data": data,
    }
