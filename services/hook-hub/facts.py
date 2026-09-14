"""Canonical, payload-free hook observation facts.

The hub produces these facts; collectors consume them from Bloodbank. No
observation passes through native hook dispatch or the original publisher.
"""
from __future__ import annotations

import json
import hashlib
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

AGENT_HOOKS = Path(__file__).resolve().parents[1] / "agent-hooks"
if str(AGENT_HOOKS) not in sys.path:
    sys.path.insert(0, str(AGENT_HOOKS))

from core.envelope import build_envelope

INVOCATION_TYPE = "bloodbank.agent.hook.updated"
SNAPSHOT_TYPE = "bloodbank.system.hook.updated"
# Leave room for transport headers under the deployed 1 MiB broker maximum.
MAX_FACT_BYTES = 900_000
MAX_TIMELINE = 512
HEARTBEAT_KEYS = ("schema_version", "generated_at", "hub", "totals", "native_activity",
                  "handler_activity", "observed_since")


def configuration_fingerprint(snapshot: dict) -> str:
    configuration = {
        "bindings": [{key: value for key, value in binding.items()
                      if key not in {"activity", "observed_state", "state"}}
                     for binding in snapshot["bindings"]],
        "handlers": snapshot["handlers"],
        "inventory": {key: value for key, value in snapshot.get("installed_inventory", {}).items()
                      if key != "generated_at"},
    }
    return hashlib.sha256(json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stable_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "33god:hook:" + value))


def envelope(hub_id: str, sequence: int, ce_type: str, data: dict,
             observed_at: str) -> dict:
    """One stable event identity and timestamp, retained verbatim on retries."""
    event_id = str(uuid.uuid5(uuid.UUID(hub_id), f"{ce_type}:{sequence}"))
    invocation = data.get("invocation")
    iid = invocation["invocation_id"] if invocation else hub_id
    result = build_envelope(
        ce_type=ce_type, source=f"urn:33god:service:hook-hub:{hub_id}",
        producer="hook-hub", service="hook-hub",
        actor={"type": "service", "agent_id": "hook-hub", **(
            {"cli": invocation["cli"]} if invocation else {})},
        data={"schema_version": 1, "hub_id": hub_id,
              "sequence": sequence, "revision": sequence, **data},
        event_id=event_id, correlation_id=stable_uuid(iid),
        causation_id=stable_uuid(iid),
        ordering_key=f"hook:{hub_id}:{iid}",
        validate=False,
    )
    result["time"] = observed_at
    return result


def serialize(value: dict) -> str:
    body = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    if len(body.encode()) > MAX_FACT_BYTES:
        raise ValueError("hook_observation_exceeds_transport_limit")
    return body


def expires_at(observed_at: str, ttl: int = 90) -> str:
    return (datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            + timedelta(seconds=ttl)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def snapshot_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Select schema-declared metadata; never forward arbitrary config keys.

    The shared schema is the field allowlist, including deeply nested installed
    sources. Commands, environment values, hook input and output are not fields
    in that contract and cannot leak when an inventory collector adds a key.
    """
    schema_path = Path(__file__).resolve().parents[2] / "schemas/_common/hook_observability.v1.json"
    schema = json.loads(schema_path.read_text())

    def project(value: Any, rule: dict) -> Any:
        if "$ref" in rule:
            rule = schema["$defs"][rule["$ref"].rsplit("/", 1)[-1]]
        if value is None:
            return None
        if isinstance(value, dict):
            properties = rule.get("properties", {})
            extra = rule.get("additionalProperties")
            return {key: project(item, properties.get(key, extra))
                    for key, item in value.items()
                    if key in properties or isinstance(extra, dict)}
        if isinstance(value, list):
            return [project(item, rule["items"]) for item in value]
        return value

    return project(snapshot, schema["$defs"]["snapshot"])
