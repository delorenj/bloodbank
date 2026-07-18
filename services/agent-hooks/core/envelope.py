"""CloudEvents 1.0 envelope builder for Bloodbank agent hooks (v1 contract).

Envelope shape follows bloodbank/schemas/_common/cloudevent_base.v1.json and
the Bloodbank Event Naming Contract v1 at bloodbank/docs/event-naming.md.

Every envelope is contract-checked by core.validate.assert_contract before it
leaves this module. Optional jsonschema validation against the matching
schema (bloodbank/schemas/bloodbank/v1/<domain>/<entity>.<action>.v1.json)
runs when BLOODBANK_HOOK_VALIDATE=1 (or validate=True).

The `dataschema` URI keeps the `holyfields` Apicurio namespace literal per
docs/event-naming.md §13 — that key is the registry artifact id, independent
of where the source-of-truth schemas live on disk.

There is no legacy 3-token path. Anything that does not match
bloodbank.v1.<domain>.<entity>.<action> raises ContractViolation at build
time.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from .validate import (
    ContractViolation,
    KIND_MARKERS,
    assert_contract,
    schema_identity_for,
    subject_for,
)

# W3C Trace Context zero-trace placeholder.
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
    kind: str = "event",
    source: str,
    producer: str,
    service: str,
    actor: dict,
    data: Any,
    correlation_id: str,
    causation_id: str,
    subject: str | None = None,
    event_id: str | None = None,
    ordering_key: str | None = None,
    command_id: str | None = None,
    idempotency_key: str | None = None,
    delivery: str | None = None,
    schemaref: str | None = None,
    dataschema: str | None = None,
    traceparent: str | None = None,
    validate: bool | None = None,
) -> dict:
    """Build a v1 CloudEvents envelope.

    Mandatory:
        ce_type           — bloodbank.v1.<domain>.<entity>.<action>
        kind              — "event" | "command" | "reply"
        source, producer, service — CloudEvents identity
        actor             — {type, agent_id, [cli, provider, model]} per §10
        data              — payload
        correlation_id    — UUID; root events use self-id
        causation_id      — UUID of the prior event/command

    Kind-specific:
        kind=event   → ordering_key required
        kind=command → command_id, idempotency_key required (delivery defaults to single_consumer)
        kind=reply   → causation_id of the originating command

    Auto-derived when omitted:
        subject     — subject_for(ce_type, kind)
        event_id    — new UUID
        delivery    — "single_consumer" for kind=command
        dataschema  — apicurio://holyfields/<ce_type>/versions/1
        schemaref   — <ce_type>.v1
        traceparent — ZERO_TRACEPARENT

    Raises ContractViolation on any contract failure.
    """
    if not correlation_id:
        raise ContractViolation("correlation_id is required (§11)")
    if not causation_id:
        raise ContractViolation("causation_id is required (§11)")
    if kind not in KIND_MARKERS:
        raise ContractViolation(f"kind {kind!r} must be event|command|reply (§4)")

    # Derive domain from type segment 3. assert_type_shape inside
    # assert_contract will re-validate but we need the value now to populate
    # envelope.domain.
    parts = ce_type.split(".")
    if len(parts) != 5:
        raise ContractViolation(
            f"type {ce_type!r} must be exactly 5 dotted tokens (§2)"
        )
    domain = parts[2]

    if subject is None:
        subject = subject_for(ce_type, kind)
    schema_identity = schema_identity_for(ce_type, kind)

    envelope = {
        "specversion": "1.0",
        "id": event_id or new_uuid(),
        "source": source,
        "type": ce_type,
        "subject": subject,
        "time": now_iso(),
        "datacontenttype": "application/json",
        "dataschema": dataschema
        or f"apicurio://holyfields/{schema_identity}/versions/1",
        "correlationid": correlation_id,
        "causationid": causation_id,
        "producer": producer,
        "service": service,
        "domain": domain,
        "schemaref": schemaref or f"{schema_identity}.v1",
        "traceparent": traceparent or ZERO_TRACEPARENT,
        "kind": kind,
        "actor": actor,
        "data": data,
    }

    if kind == "event":
        if not ordering_key:
            raise ContractViolation("ordering_key is required for kind=event (§11.1)")
        envelope["ordering_key"] = ordering_key
    elif kind == "command":
        if not command_id:
            raise ContractViolation("command_id is required for kind=command (§11)")
        if not idempotency_key:
            raise ContractViolation(
                "idempotency_key is required for kind=command (§11.2)"
            )
        envelope["command_id"] = command_id
        envelope["idempotency_key"] = idempotency_key
        envelope["delivery"] = delivery or "single_consumer"
    # kind=reply has no additional required fields beyond causation_id.

    # Always run stdlib contract checks; loud failure on violation.
    assert_contract(envelope)

    # Optional JSON Schema validation (jsonschema-dependent).
    if validate is None:
        validate = os.environ.get("BLOODBANK_HOOK_VALIDATE") == "1"
    if validate:
        from .validate import validate_envelope  # noqa: WPS433

        validate_envelope(envelope)

    return envelope
