"""Bloodbank command validation, profile routing, and lifecycle envelopes.

This module deliberately has no Hermes or NATS imports.  It is the fail-closed
boundary between an untrusted broker message and the Hermes gateway adapter.
"""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

logger = logging.getLogger(__name__)




@dataclass(frozen=True)
class LaneSpec:
    """One command delivery lane: a command type, its subject and a lane name.

    The lanes carry the same ``data`` payload.  They differ only in which
    durable consumer (and therefore which in-flight slots) they are delivered
    through, so a human-facing command is never queued behind batch work.
    """

    name: str
    command_type: str
    subject: str


BATCH = LaneSpec(
    "batch",
    "bloodbank.agent.invocation.start",
    "bloodbank.cmd.agent.invocation.start",
)
INTERACTIVE = LaneSpec(
    "interactive",
    "bloodbank.agent.invocation.invoke",
    "bloodbank.cmd.agent.invocation.invoke",
)
LANES = {lane.name: lane for lane in (BATCH, INTERACTIVE)}

# The batch lane is the original single lane; keep these importable.
COMMAND_TYPE = BATCH.command_type
COMMAND_SUBJECT = BATCH.subject
COMMAND_STREAM = "BLOODBANK_COMMANDS"
EVENT_STREAM = "BLOODBANK_EVENTS"

TURN_STARTED = "bloodbank.conversation.turn.started"
INVOCATION_STARTED = "bloodbank.agent.invocation.started"
INVOCATION_COMPLETED = "bloodbank.agent.invocation.completed"
INVOCATION_FAILED = "bloodbank.agent.invocation.failed"
# error_code stamped on invocation.failed when a lane's per-turn wall clock
# expired and the gateway stopped the Hermes turn.
TURN_TIMEOUT_CODE = "turn_timeout"
# timeout_source of a turn_timeout whose turn had detached at the lane limit
# and was still running at the lane's detached ceiling.
DETACHED_CEILING_SOURCE = "detached_ceiling"
TURN_COMPLETED = "bloodbank.conversation.turn.completed"
# One interim assistant message ("I'll check the board first.") of a running
# command turn: what the agent said it is doing, while the human waits.
INVOCATION_PROGRESSED = "bloodbank.agent.invocation.progressed"
# A running turn outlived its lane's wall clock and gave its slot back instead
# of being stopped (extra.interactive.detach_on_timeout). Not terminal.
INVOCATION_DETACHED = "bloodbank.agent.invocation.detached"
PROGRESS_TEXT_MAX_CHARS = 500

_UUID_NAMESPACE = uuid.UUID("633de934-f359-50f8-978f-3ef4ebbdac69")
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
# Keep registry identities aligned with the fleet's canonical slug shape:
# lowercase ASCII alphanumerics and dashes, with no leading/trailing dash.
# Registry keys are evidence, not user input, so noncanonical spellings must
# invalidate the snapshot rather than be silently normalized into an identity.
_CANONICAL_AGENT_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
# data.model: one Hermes model route for this turn only ("automaticai/auto/fast",
# "automaticai/personal/claude-sonnet-5.5"). It becomes the argument of an
# internal `/model <route> --once`, so it is one token: no whitespace (no extra
# flags such as --global), no leading "-" or "/".
_MODEL_ROUTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")


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


def _positive_number(value: Any) -> bool:
    """True for a finite number > 0 (a JSON bool is not a number here)."""

    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


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


def decode_command(
    payload: bytes, *, max_bytes: int = 262_144, lane: LaneSpec = BATCH
) -> dict[str, Any]:
    """Decode and validate the canonical invocation command envelope.

    ``lane`` selects the command type the envelope must carry: a ``start``
    envelope delivered on the interactive subject (or the reverse) is poison.

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
    if envelope["type"] != lane.command_type:
        raise CommandInvalid(f"type must be {lane.command_type!r}")
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
    deadline = data.get("deadline_seconds")
    if deadline is not None and not _positive_number(deadline):
        raise CommandInvalid("data.deadline_seconds must be null or a positive number")
    model = data.get("model")
    if model is not None and (
        not isinstance(model, str) or _MODEL_ROUTE.fullmatch(model) is None
    ):
        raise CommandInvalid(
            "data.model must be null or one model route (letters, digits and . _ : / @ + -, "
            "no spaces, at most 200 characters, not starting with - or /)"
        )

    expected_schema = f"apicurio://holyfields/{lane.command_type}/versions/1"
    if "dataschema" in envelope and envelope["dataschema"] != expected_schema:
        raise CommandInvalid(f"dataschema must be {expected_schema!r}")
    expected_ref = f"{lane.command_type}.v1"
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
            normalized_agent_id = agent_id.strip().lower()
            if (
                agent_id != normalized_agent_id
                or _CANONICAL_AGENT_ID.fullmatch(agent_id) is None
            ):
                raise RegistryInvalid(
                    "fleet registry agent identifiers must be canonical lowercase slugs"
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
                and registry_bloodbank_enabled(agent_id, bloodbank)
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



def registry_bloodbank_enabled(agent_id: str, bloodbank: Mapping[str, Any]) -> bool:
    """Return the effective activation of one registry row's Bloodbank block.

    No key means enabled: an ABSENT ``bloodbank.enabled`` activates the row.
    An explicit ``false`` quarantines it and an explicit ``true`` enables it.
    A present value that is not a YAML boolean (``"true"``, ``yes`` parsed as a
    string, ``null``, ``1``) is invalid: the row is treated as disabled and the
    gateway logs a loud error naming the field, so a typo never silently
    widens or narrows dispatch.
    """
    if "enabled" not in bloodbank:
        return True
    value = bloodbank["enabled"]
    if value is True or value is False:
        return value
    logger.error(
        "fleet registry agents.%s.bloodbank.enabled must be a strict YAML "
        "boolean (got %s); treating the agent as disabled",
        agent_id,
        type(value).__name__,
    )
    return False

@dataclass(frozen=True)
class Invocation:
    envelope: dict[str, Any]
    profile: str
    target_agent_id: str
    prompt: str
    thread_id: str
    turn_id: str
    invocation_id: str
    lane: str = BATCH.name
    # data.model: the model route this turn alone runs on (None = the profile's).
    model: str | None = None

    @classmethod
    def from_envelope(
        cls, envelope: dict[str, Any], profile: str, lane: str = BATCH.name
    ) -> "Invocation":
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
            lane=lane,
            model=data.get("model") or None,
        )

    def event_id(self, event_type: str) -> str:
        return str(uuid.uuid5(_UUID_NAMESPACE, f"{self.invocation_id}:{event_type}"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _subject_for(event_type: str) -> str:
    _, domain, entity, action = event_type.split(".")
    return f"bloodbank.evt.{domain}.{entity}.{action}"


def lifecycle_event(
    invocation: Invocation,
    event_type: str,
    *,
    causation_id: str,
    data: dict[str, Any],
    occurred_at: str | None = None,
) -> dict[str, Any]:
    domain = event_type.split(".")[1]
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
            "lane": invocation.lane,
        },
    }


def _command_context(invocation: Invocation) -> dict[str, Any]:
    """Echo the command's ``data.context`` onto invocation lifecycle events.

    Downstream consumers (the n8n ticket workflows) act on a finished ticket
    turn statelessly: ``context.reason``, ``board_id``, ``ticket_id``,
    ``workspace`` and ``ticket_key`` ride on started/completed/failed instead
    of being remembered in workflow static data keyed by correlation id.
    Additive and only present when the command carried an object context.
    """
    context = invocation.envelope.get("data", {}).get("context")
    return {"context": context} if isinstance(context, dict) else {}


def started_events(invocation: Invocation) -> tuple[dict[str, Any], dict[str, Any]]:
    turn = lifecycle_event(
        invocation,
        TURN_STARTED,
        causation_id=invocation.envelope["id"],
        data={
            "thread_id": invocation.thread_id,
            "turn_id": invocation.turn_id,
            "prompt_text": invocation.prompt,
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
            **_command_context(invocation),
        },
    )
    return turn, started


def progress_text(raw: Any) -> tuple[str | None, bool]:
    """(text, truncated) for a progress fact: trimmed, at most
    PROGRESS_TEXT_MAX_CHARS characters (an ellipsis marks a cut), None for
    anything that is not a non-blank string."""

    if not isinstance(raw, str):
        return None, False
    text = raw.strip()
    if not text:
        return None, False
    if len(text) <= PROGRESS_TEXT_MAX_CHARS:
        return text, False
    return text[: PROGRESS_TEXT_MAX_CHARS - 1].rstrip() + "…", True


def progress_event(
    invocation: Invocation,
    *,
    seq: int,
    text: str,
    truncated: bool = False,
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    """bloodbank.agent.invocation.progressed for one interim message.

    The id is random, not derived: progress is never journaled or replayed,
    and a re-run of the same command (redelivery after a restart) must not be
    deduplicated against the first run's progress inside the stream's
    duplicate window."""

    event = lifecycle_event(
        invocation,
        INVOCATION_PROGRESSED,
        causation_id=invocation.event_id(INVOCATION_STARTED),
        data={
            "invocation_id": invocation.invocation_id,
            "thread_id": invocation.thread_id,
            "turn_id": invocation.turn_id,
            "seq": seq,
            "text": text,
            "text_truncated": truncated,
            "elapsed_seconds": (
                round(max(0.0, elapsed_seconds), 1)
                if elapsed_seconds is not None
                else None
            ),
            **_command_context(invocation),
        },
    )
    event["id"] = str(uuid.uuid4())
    return event


def detached_event(
    invocation: Invocation,
    *,
    elapsed_seconds: float,
    timeout_seconds: float,
    timeout_source: str,
    ceiling_seconds: float,
    detached_count: int,
    max_detached: int,
) -> dict[str, Any]:
    """bloodbank.agent.invocation.detached: the turn outlived its lane's wall
    clock and its slot was given back while it keeps running.

    Not terminal: the same command later closes with exactly one completed or
    failed fact (that one carries ``detached: true``); a turn still running at
    ``ceiling_seconds`` after dispatch is stopped and fails turn_timeout with
    ``timeout_source: detached_ceiling``. The id is random for the same reason
    progress ids are: a re-run of the command after a restart detaches anew."""

    event = lifecycle_event(
        invocation,
        INVOCATION_DETACHED,
        causation_id=invocation.event_id(INVOCATION_STARTED),
        data={
            "invocation_id": invocation.invocation_id,
            "thread_id": invocation.thread_id,
            "turn_id": invocation.turn_id,
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 1),
            "timeout_seconds": round(timeout_seconds, 1),
            "timeout_source": timeout_source,
            "ceiling_seconds": round(ceiling_seconds, 1),
            "detached_count": detached_count,
            "max_detached": max_detached,
            **_command_context(invocation),
        },
    )
    event["id"] = str(uuid.uuid4())
    return event


def terminal_events(
    invocation: Invocation,
    *,
    outcome: str,
    failure_code: str | None = None,
    failure_message: str | None = None,
    failure_data: dict[str, Any] | None = None,
    occurred_at: str | None = None,
    extra_data: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the terminal invocation fact and its closing turn fact.

    ``failure_data`` adds optional fields to a failed invocation payload (the
    turn-timeout path carries ``elapsed_seconds``/``timeout_seconds``/...);
    ``extra_data`` adds optional fields to either outcome (a turn that detached
    at its lane's wall clock carries ``detached``/``detached_after_seconds``/
    ``elapsed_seconds``). Neither can override the fields the contract owns."""

    started_id = invocation.event_id(INVOCATION_STARTED)
    if outcome == "success":
        terminal = lifecycle_event(
            invocation,
            INVOCATION_COMPLETED,
            causation_id=started_id,
            occurred_at=occurred_at,
            data={
                **(extra_data or {}),
                "invocation_id": invocation.invocation_id,
                "thread_id": invocation.thread_id,
                "turn_id": invocation.turn_id,
                **_command_context(invocation),
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
                **(extra_data or {}),
                **(failure_data or {}),
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
                **_command_context(invocation),
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
