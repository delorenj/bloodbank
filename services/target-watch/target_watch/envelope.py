"""CloudEvents envelopes for what target-watch publishes, and their deterministic ids."""
from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime

from .paths import repo_root

# The published contract (watch schemas, smoketest-watch-contracts.py): uuid5(NAMESPACE_URL, ...).
NAMESPACE = uuid.NAMESPACE_URL
SERVICE = "target-watch"
SOURCE = "urn:33god:service:target-watch"
ACTOR = {"type": "service", "agent_id": "bloodbank.service.target-watch"}

DETECTED = "bloodbank.watch.target.detected"
ROUTED = "bloodbank.watch.target.routed"
EMAIL_RECEIVED = "bloodbank.messaging.email.received"


def detection_id(watch_id: str, dedupe_key: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{watch_id}:{dedupe_key}"))


def routed_id(detection_event_id: str, action: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{detection_event_id}:{action}"))


def gmail_event_id(message_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"gmail:{message_id}"))


def is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def rfc3339(moment: datetime | None = None) -> str:
    moment = (moment or datetime.now(UTC)).astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def subject_for(ce_type: str) -> str:
    return "bloodbank.evt." + ce_type.removeprefix("bloodbank.")


def build(
    ce_type: str,
    data: dict,
    *,
    event_id: str,
    ordering_key: str,
    correlationid: str | None = None,
    causationid: str | None = None,
    now: datetime | None = None,
) -> dict:
    domain = ce_type.split(".")[1]
    return {
        "specversion": "1.0",
        "id": event_id,
        "source": SOURCE,
        "type": ce_type,
        "subject": subject_for(ce_type),
        "time": rfc3339(now),
        "datacontenttype": "application/json",
        "dataschema": f"apicurio://holyfields/{ce_type}/versions/1",
        "schemaref": f"{ce_type}.v1",
        "correlationid": correlationid if is_uuid(correlationid) else event_id,
        "causationid": causationid if is_uuid(causationid) else None,
        "producer": SERVICE,
        "service": SERVICE,
        "domain": domain,
        "kind": "event",
        "actor": dict(ACTOR),
        "ordering_key": ordering_key,
        "data": data,
    }


def _validator():
    hooks = str(repo_root() / "services" / "agent-hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    from core import validate  # noqa: PLC0415 - the repo's single enforcement point

    return validate


def validate(envelope: dict) -> None:
    """Naming contract + JSON Schema, through services/agent-hooks/core/validate.py."""
    _validator().validate_envelope(envelope)
