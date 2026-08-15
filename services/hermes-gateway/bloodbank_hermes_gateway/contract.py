"""Bloodbank command validation, profile routing, and lifecycle envelopes.

This module deliberately has no Hermes or NATS imports.  It is the fail-closed
boundary between an untrusted broker message and the Hermes gateway adapter.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml


COMMAND_TYPE = "bloodbank.v1.agent.invocation.start"
COMMAND_SUBJECT = "bloodbank.cmd.v1.agent.invocation.start"
COMMAND_STREAM = "BLOODBANK_COMMANDS"
EVENT_STREAM = "BLOODBANK_EVENTS"

TURN_STARTED = "bloodbank.v1.conversation.turn.started"
INVOCATION_STARTED = "bloodbank.v1.agent.invocation.started"
INVOCATION_COMPLETED = "bloodbank.v1.agent.invocation.completed"
INVOCATION_FAILED = "bloodbank.v1.agent.invocation.failed"
TURN_COMPLETED = "bloodbank.v1.conversation.turn.completed"

_UUID_NAMESPACE = uuid.UUID("633de934-f359-50f8-978f-3ef4ebbdac69")
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class CommandInvalid(ValueError):
    """The broker message is poison and must be terminally acknowledged."""


class RouteInvalid(CommandInvalid):
    """The requested target cannot safely resolve to a Hermes profile."""


class RegistryInvalid(RuntimeError):
    """The configured fleet registry cannot be read safely."""


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommandInvalid(f"{field} must be a non-empty string")
    return value.strip()


def _uuid_string(value: Any, field: str) -> str:
    text = _nonempty_string(value, field)
    try:
        uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise CommandInvalid(f"{field} must be an RFC 4122 UUID") from exc
    return text


def _timestamp(value: Any) -> str:
    text = _nonempty_string(value, "time")
    if not _RFC3339.fullmatch(text):
        raise CommandInvalid("time must be an RFC 3339 timestamp")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CommandInvalid("time must be an RFC 3339 timestamp") from exc
    return text


def decode_command(payload: bytes, *, max_bytes: int = 262_144) -> dict[str, Any]:
    """Decode and validate the canonical invocation command envelope.

    The canonical schema permits a null prompt because it describes the wire
    family.  This consumer needs executable content, so an empty/null prompt is
    terminally invalid at this adapter boundary.
    """

    if not isinstance(payload, bytes):
        raise CommandInvalid("payload must be bytes")
    if len(payload) > max_bytes:
        raise CommandInvalid(f"payload exceeds max_command_bytes ({max_bytes})")
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommandInvalid("payload must be UTF-8 JSON") from exc
    if not isinstance(envelope, dict):
        raise CommandInvalid("envelope must be a JSON object")

    required_strings = (
        "id",
        "source",
        "type",
        "time",
        "correlationid",
        "producer",
        "service",
        "domain",
        "kind",
        "command_id",
        "idempotency_key",
        "delivery",
    )
    for field in required_strings:
        _nonempty_string(envelope.get(field), field)

    if envelope.get("specversion") != "1.0":
        raise CommandInvalid("specversion must be '1.0'")
    if envelope["type"] != COMMAND_TYPE:
        raise CommandInvalid(f"type must be {COMMAND_TYPE!r}")
    if envelope["kind"] != "command":
        raise CommandInvalid("kind must be 'command'")
    if envelope["domain"] != "agent":
        raise CommandInvalid("domain must be 'agent'")
    if envelope["delivery"] != "single_consumer":
        raise CommandInvalid("delivery must be 'single_consumer'")
    if envelope.get("datacontenttype", "application/json") != "application/json":
        raise CommandInvalid("datacontenttype must be 'application/json'")

    _uuid_string(envelope["id"], "id")
    _uuid_string(envelope["correlationid"], "correlationid")
    _uuid_string(envelope["command_id"], "command_id")
    causation = envelope.get("causationid")
    if causation is not None:
        _uuid_string(causation, "causationid")
    _timestamp(envelope["time"])

    actor = envelope.get("actor")
    if not isinstance(actor, dict):
        raise CommandInvalid("actor must be an object")
    _nonempty_string(actor.get("type"), "actor.type")
    _nonempty_string(actor.get("agent_id"), "actor.agent_id")

    data = envelope.get("data")
    if not isinstance(data, dict):
        raise CommandInvalid("data must be an object")
    _nonempty_string(data.get("target_agent_id"), "data.target_agent_id")
    _nonempty_string(data.get("prompt"), "data.prompt")
    for field in ("thread_id", "turn_id"):
        value = data.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise CommandInvalid(f"data.{field} must be null or a non-empty string")
    context = data.get("context")
    if context is not None and not isinstance(context, dict):
        raise CommandInvalid("data.context must be null or an object")

    expected_schema = f"apicurio://holyfields/{COMMAND_TYPE}/versions/1"
    if "dataschema" in envelope and envelope["dataschema"] != expected_schema:
        raise CommandInvalid(f"dataschema must be {expected_schema!r}")
    expected_ref = f"{COMMAND_TYPE}.v1"
    if "schemaref" in envelope and envelope["schemaref"] != expected_ref:
        raise CommandInvalid(f"schemaref must be {expected_ref!r}")

    return envelope


class ProfileResolver:
    """Resolve an external agent ID to one existing Hermes profile."""

    def __init__(
        self,
        *,
        target_profiles: Mapping[str, str] | None,
        fleet_registry: Path,
        allow_direct_profile_targets: bool,
        normalize_profile_name: Callable[[str], str],
        validate_profile_name: Callable[[str], Any],
        profile_exists: Callable[[str], bool],
    ) -> None:
        self.target_profiles = dict(target_profiles or {})
        self.fleet_registry = fleet_registry
        self.allow_direct_profile_targets = allow_direct_profile_targets
        self.normalize_profile_name = normalize_profile_name
        self.validate_profile_name = validate_profile_name
        self.profile_exists = profile_exists

    def _fleet_mapping(self) -> tuple[dict[str, str], frozenset[str]]:
        if not self.fleet_registry.exists():
            raise RegistryInvalid("fleet registry is missing")
        try:
            parsed = yaml.safe_load(self.fleet_registry.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RegistryInvalid("fleet registry is unreadable or invalid") from exc
        if not isinstance(parsed, dict):
            raise RegistryInvalid("fleet registry root must be a mapping")
        schema_version = parsed.get("schema_version")
        if type(schema_version) is not int or schema_version != 1:
            raise RegistryInvalid("fleet registry schema_version must be exactly 1")
        if "agents" not in parsed or not isinstance(parsed["agents"], dict):
            raise RegistryInvalid("fleet registry agents must be a mapping")
        mapping: dict[str, str] = {}
        registered_targets: set[str] = set()
        for agent_id, metadata in parsed["agents"].items():
            if not isinstance(agent_id, str) or not agent_id.strip():
                raise RegistryInvalid(
                    "fleet registry agent identifiers must be non-empty strings"
                )
            registered_targets.add(agent_id)
            if not isinstance(metadata, dict):
                raise RegistryInvalid(
                    f"fleet registry metadata for {agent_id!r} must be a mapping"
                )
            profile = metadata.get("profile_name")
            bloodbank = metadata.get("bloodbank")
            if (
                isinstance(profile, str)
                and profile.strip()
                and isinstance(bloodbank, dict)
                and bloodbank.get("enabled") is True
                and bloodbank.get("gateway_scope") == "fleet"
                and bloodbank.get("target_agent_id") == agent_id
            ):
                mapping[agent_id] = profile.strip()
        return mapping, frozenset(registered_targets)

    def resolve(self, target_agent_id: str) -> str:
        target = _nonempty_string(target_agent_id, "data.target_agent_id")
        mapped = self.target_profiles.get(target)
        if mapped is None:
            fleet_mapping, registered_targets = self._fleet_mapping()
            mapped = fleet_mapping.get(target)
            if mapped is None and target in registered_targets:
                raise RouteInvalid(
                    f"target_agent_id {target!r} is registry-defined but not eligible"
                )

        if mapped is None and self.allow_direct_profile_targets:
            normalized_direct = self.normalize_profile_name(target)
            if normalized_direct == target and self.profile_exists(target):
                mapped = target

        if mapped is None:
            raise RouteInvalid(
                f"target_agent_id {target!r} has no configured profile route"
            )
        if not isinstance(mapped, str) or not mapped.strip():
            raise RouteInvalid(f"target_agent_id {target!r} maps to an invalid profile")

        normalized = self.normalize_profile_name(mapped.strip())
        try:
            self.validate_profile_name(normalized)
        except Exception as exc:
            raise RouteInvalid(
                f"target_agent_id {target!r} maps to an invalid profile"
            ) from exc
        if not self.profile_exists(normalized):
            raise RouteInvalid(f"target_agent_id {target!r} maps to a missing profile")
        return normalized


@dataclass(frozen=True)
class Invocation:
    envelope: dict[str, Any]
    profile: str
    target_agent_id: str
    prompt: str
    thread_id: str
    turn_id: str
    invocation_id: str

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any], profile: str) -> "Invocation":
        data = envelope["data"]
        command_id = envelope["command_id"]
        correlation_id = envelope["correlationid"]
        return cls(
            envelope=envelope,
            profile=profile,
            target_agent_id=data["target_agent_id"].strip(),
            prompt=data["prompt"].strip(),
            thread_id=(data.get("thread_id") or f"bloodbank:{correlation_id}").strip(),
            turn_id=(data.get("turn_id") or command_id).strip(),
            invocation_id=command_id,
        )

    def event_id(self, event_type: str) -> str:
        return str(uuid.uuid5(_UUID_NAMESPACE, f"{self.invocation_id}:{event_type}"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _subject_for(event_type: str) -> str:
    _, version, domain, entity, action = event_type.split(".")
    return f"bloodbank.evt.{version}.{domain}.{entity}.{action}"


def lifecycle_event(
    invocation: Invocation,
    event_type: str,
    *,
    causation_id: str,
    data: dict[str, Any],
    occurred_at: str | None = None,
) -> dict[str, Any]:
    domain = event_type.split(".")[2]
    event_id = invocation.event_id(event_type)
    return {
        "specversion": "1.0",
        "id": event_id,
        "source": "urn:33god:service:bloodbank-hermes-gateway",
        "type": event_type,
        "subject": _subject_for(event_type),
        "time": _timestamp(occurred_at) if occurred_at is not None else _now(),
        "datacontenttype": "application/json",
        "dataschema": f"apicurio://holyfields/{event_type}/versions/1",
        "correlationid": invocation.envelope["correlationid"],
        "causationid": causation_id,
        "producer": f"hermes-agent:{invocation.profile}",
        "service": "bloodbank-hermes-gateway",
        "domain": domain,
        "schemaref": f"{event_type}.v1",
        "kind": "event",
        "actor": {
            "type": "agent_api",
            "agent_id": invocation.target_agent_id,
            "cli": "hermes",
            "provider": None,
            "model": None,
        },
        "ordering_key": f"turn:{invocation.turn_id}",
        "data": {
            **data,
            "command_id": invocation.envelope["command_id"],
            "idempotency_key": invocation.envelope["idempotency_key"],
            "target_agent_id": invocation.target_agent_id,
            "profile_name": invocation.profile,
        },
    }


def started_events(invocation: Invocation) -> tuple[dict[str, Any], dict[str, Any]]:
    turn = lifecycle_event(
        invocation,
        TURN_STARTED,
        causation_id=invocation.envelope["id"],
        data={
            "thread_id": invocation.thread_id,
            "turn_id": invocation.turn_id,
            "prompt_text": None,
        },
    )
    started = lifecycle_event(
        invocation,
        INVOCATION_STARTED,
        causation_id=turn["id"],
        data={
            "invocation_id": invocation.invocation_id,
            "thread_id": invocation.thread_id,
            "turn_id": invocation.turn_id,
            "parent_invocation_id": None,
        },
    )
    return turn, started


def terminal_events(
    invocation: Invocation,
    *,
    outcome: str,
    failure_code: str | None = None,
    failure_message: str | None = None,
    occurred_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started_id = invocation.event_id(INVOCATION_STARTED)
    if outcome == "success":
        terminal = lifecycle_event(
            invocation,
            INVOCATION_COMPLETED,
            causation_id=started_id,
            occurred_at=occurred_at,
            data={
                "invocation_id": invocation.invocation_id,
                "thread_id": invocation.thread_id,
                "turn_id": invocation.turn_id,
            },
        )
        turn_outcome = "completed"
    else:
        cancelled = outcome == "cancelled"
        terminal = lifecycle_event(
            invocation,
            INVOCATION_FAILED,
            causation_id=started_id,
            occurred_at=occurred_at,
            data={
                "invocation_id": invocation.invocation_id,
                "thread_id": invocation.thread_id,
                "turn_id": invocation.turn_id,
                "error_code": (
                    "processing_cancelled"
                    if cancelled
                    else failure_code or "processing_failed"
                ),
                "error_message": (
                    "Hermes processing was cancelled"
                    if cancelled
                    else failure_message
                    or "Hermes processing did not complete successfully"
                ),
            },
        )
        turn_outcome = "canceled" if cancelled else "failed"

    turn = lifecycle_event(
        invocation,
        TURN_COMPLETED,
        causation_id=terminal["id"],
        occurred_at=occurred_at,
        data={
            "thread_id": invocation.thread_id,
            "turn_id": invocation.turn_id,
            "outcome": turn_outcome,
        },
    )
    return terminal, turn
