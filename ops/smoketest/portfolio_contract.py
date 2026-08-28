"""Deterministic portfolio-envelope builder shared by contract and transport tests.

This is test tooling, not a Director publisher. It binds payload lineage to the
CloudEvents extensions exactly as a real publisher must before transport.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "ops" / "fixtures" / "portfolio-contracts.v1.json"
PAYLOADS = json.loads(FIXTURE_PATH.read_text())
RECEIPT_TYPE = "bloodbank.portfolio.receipt.recorded"


def build_envelope(
    ce_type: str,
    *,
    correlation_id: str | None = None,
    event_id: str | None = None,
    occurred_at: str | None = None,
) -> dict:
    """Build a complete envelope from the tracked payload fixture."""
    from core.validate import subject_for

    data = copy.deepcopy(PAYLOADS[ce_type])
    if correlation_id is not None:
        data["correlation_id"] = correlation_id
    if occurred_at is not None:
        data["occurred_at"] = occurred_at

    resolved_event_id = event_id or str(
        uuid.uuid5(uuid.NAMESPACE_URL, ce_type + data["idempotency_key"])
    )
    if ce_type == RECEIPT_TYPE:
        if event_id is not None:
            data["receipt_id"] = event_id
        resolved_event_id = data["receipt_id"]

    return {
        "specversion": "1.0",
        "id": resolved_event_id,
        "source": "urn:33god:agent:delonet-director",
        "type": ce_type,
        "subject": subject_for(ce_type, "event"),
        "time": data["occurred_at"],
        "datacontenttype": "application/json",
        "dataschema": f"apicurio://holyfields/{ce_type}/versions/1",
        "correlationid": data["correlation_id"],
        "causationid": data["causation_id"],
        "producer": "delonet-director",
        "service": "delonet-director",
        "domain": "portfolio",
        "schemaref": f"{ce_type}.v1",
        "kind": "event",
        "actor": {"type": "agent_api", "agent_id": "delonet-director"},
        "ordering_key": f"portfolio:{data['portfolio_id']}",
        "idempotency_key": data["idempotency_key"],
        "data": data,
    }
