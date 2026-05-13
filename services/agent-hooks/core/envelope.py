"""CloudEvents 1.0 envelope builder for Bloodbank agent hooks.

Envelope shape follows holyfields/schemas/_common/cloudevent_base.v1.json.
Per bloodbank/CLAUDE.md, every event MUST carry correlationid and
causationid — this builder enforces it.

Optional runtime validation against the corresponding holyfields schema is
available via core.validate.validate_envelope(); enabled when
BLOODBANK_HOOK_VALIDATE=1 or when callers pass validate=True. Default off so
hook publishers stay stdlib-only.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

# W3C Trace Context zero-trace placeholder. Replaced by real trace ids when
# producers integrate with OpenTelemetry; for now this satisfies the
# traceparent field's pattern in cloudevent_base.v1.json.
ZERO_TRACEPARENT = "00-00000000000000000000000000000000-0000000000000000-00"


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
    dataschema: str | None = None,
    traceparent: str | None = None,
    validate: bool | None = None,
) -> dict:
    """Build a CloudEvents 1.0 envelope.

    correlation_id and causation_id are mandatory. For the first event in a
    chain (e.g. a session-start) callers should pass causation_id == correlation_id
    so the chain self-roots cleanly.

    ``dataschema`` defaults to ``apicurio://holyfields/<ce_type>/versions/1``.
    ``traceparent`` defaults to the zero-trace placeholder.
    ``validate`` opts into runtime JSON Schema validation; if None, reads
    ``BLOODBANK_HOOK_VALIDATE`` from the env (``1`` enables).
    """
    if not correlation_id:
        raise ValueError("correlation_id is required")
    if not causation_id:
        raise ValueError("causation_id is required")
    envelope = {
        "specversion": "1.0",
        "id": event_id or new_uuid(),
        "source": source,
        "type": ce_type,
        "subject": subject,
        "time": now_iso(),
        "datacontenttype": "application/json",
        "dataschema": dataschema or f"apicurio://holyfields/{ce_type}/versions/1",
        "correlationid": correlation_id,
        "causationid": causation_id,
        "producer": producer,
        "service": service,
        "domain": domain,
        "schemaref": schemaref or f"{ce_type}.v1",
        "traceparent": traceparent or ZERO_TRACEPARENT,
        "data": data,
    }

    if validate is None:
        validate = os.environ.get("BLOODBANK_HOOK_VALIDATE") == "1"
    if validate:
        # Imported lazily so envelope construction never fails when jsonschema
        # isn't installed and validation isn't requested.
        from .validate import validate_envelope  # noqa: WPS433
        validate_envelope(envelope)

    return envelope
