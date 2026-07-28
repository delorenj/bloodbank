"""Bloodbank Event Naming Contract v1 validation.

This module is the runtime enforcement point for bloodbank/docs/event-naming.md.
It exposes two layers:

1. Stdlib-only contract assertions (assert_contract, assert_type_shape, ...)
   These run on every envelope build with zero dependencies. They cover §2
   (type regex), §3 (subject shape + kind marker), §5 (action tense),
   §6-§8 (allowlists), §9 (banned tokens), and required-field presence per
   §11.

2. Optional JSON Schema validation (validate_envelope) against the
   bloodbank/schemas/bloodbank/v1/<domain>/<entity>.<action>.v1.json schema.
   Requires jsonschema. Off by default at hook time; on in CI.

Schemas live in this repo (bloodbank/schemas/) and are the single source of
truth. A sibling holyfields/schemas/ tree is honored as a fallback during
the transition off the prior Holyfields-owned layout — that fallback will be
removed once consumers cut over.

Failures are loud. There is no quarantine or alias path — see "Hard rename,
no aliases" in docs/event-naming.md §15.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# §2 - type regex
# --------------------------------------------------------------------------

TYPE_REGEX = re.compile(
    r"^bloodbank\.v[0-9]+\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"
)

# --------------------------------------------------------------------------
# §3 - subject + kind marker
# --------------------------------------------------------------------------

KIND_MARKERS = {"event": "evt", "command": "cmd", "reply": "rpy"}
MARKER_TO_KIND = {v: k for k, v in KIND_MARKERS.items()}
SUBJECT_REGEX = re.compile(
    r"^bloodbank\.(evt|cmd|rpy)\.v[0-9]+\."
    r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"
)

# v1 retains its convention-based open registry for backward compatibility.
# Every later contract version is opt-in and maps to one reviewed schema path.
REGISTERED_NON_V1_SCHEMAS = {
    "bloodbank.v2.repo.maintenance.failed": (
        "bloodbank/v2/repo/maintenance.failed.v1.json"
    ),
}

# --------------------------------------------------------------------------
# §6, §7, §8, §9 - allowlists / banned tokens
# --------------------------------------------------------------------------

ALLOWED_DOMAINS = frozenset({
    # active
    "conversation", "agent", "llm", "cli", "system", "audio", "repo", "lifecycle",
    "finance", "attendance", "curator", "reporting",
    # reserved (registered but not yet emitted)
    "approval", "workspace", "workflow", "memory",
})

ALLOWED_ENTITIES = frozenset({
    "thread", "turn", "message",
    "invocation",
    "session", "process", "stdout", "stderr",
    "request", "response",
    "tool",
    "heartbeat",
    "decision", "intake", "task", "maintenance",
    "approval_request",
    "worktree", "branch", "diff",
    "file", "transcription",
    "mission", "checkpoint", "gate", "roadmap", "status",
    "sync", "account", "transaction", "subscription", "zombie_charge", "paycheck", "projection",
    "clock", "report",
})

EVENT_ACTIONS = frozenset({
    "created", "resumed", "started", "ended", "completed", "failed", "canceled",
    "generated", "appended", "received", "sent", "granted", "denied",
    "opened", "closed", "spawned", "exited", "checked_out",
    "requested", "invoked", "recorded", "triaged",
    "updated", "reached", "resolved",
    "detected", "flagged", "routed", "breached", "clocked_in", "clocked_out",
})

COMMAND_ACTIONS = frozenset({
    "create", "resume", "start", "end", "complete", "fail", "cancel",
    "generate", "append", "receive", "send", "grant", "deny",
    "open", "close", "spawn", "kill", "checkout", "invoke",
    "request", "toggle", "clock_in", "clock_out",
})

BANNED_TOKENS = frozenset({
    "claude", "anthropic", "copilot", "github", "openai", "gemini",
    "cursor", "opencode", "amazonq", "codex", "ollama", "llama", "mistral",
})

# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class ContractViolation(ValueError):
    """Raised when an envelope violates the v1 naming contract."""


class ValidationUnavailable(RuntimeError):
    """Raised when JSON Schema validation is requested but jsonschema is missing."""


class EnvelopeInvalid(ValueError):
    """Raised when an envelope fails JSON Schema validation against its schema."""


# --------------------------------------------------------------------------
# Stdlib-only contract assertions
# --------------------------------------------------------------------------


def _split_type(ce_type: str) -> tuple[str, str, str, str, str]:
    """Split a versioned type into (vendor, version, domain, entity, action)."""
    if not isinstance(ce_type, str):
        raise ContractViolation(f"type must be str, got {type(ce_type).__name__}")
    if not TYPE_REGEX.match(ce_type):
        raise ContractViolation(
            f"type {ce_type!r} does not match v1 regex {TYPE_REGEX.pattern!r}"
        )
    parts = ce_type.split(".")
    return parts[0], parts[1], parts[2], parts[3], parts[4]


def assert_registered_version(ce_type: str) -> None:
    """Reject non-v1 types unless their exact type has a reviewed schema."""
    _, version, _, _, _ = _split_type(ce_type)
    if version != "v1" and ce_type not in REGISTERED_NON_V1_SCHEMAS:
        raise ContractViolation(
            f"type {ce_type!r} is not registered for contract version {version!r}"
        )


def assert_type_shape(ce_type: str) -> tuple[str, str, str]:
    """Assert §2. Returns (domain, entity, action) on success."""
    _, _, domain, entity, action = _split_type(ce_type)
    if domain not in ALLOWED_DOMAINS:
        raise ContractViolation(
            f"domain {domain!r} not in v1 allowlist (§6); see docs/event-naming.md"
        )
    if entity not in ALLOWED_ENTITIES:
        raise ContractViolation(
            f"entity {entity!r} not in v1 allowlist (§7); see docs/event-naming.md"
        )
    return domain, entity, action


def assert_banned_tokens(ce_type: str) -> None:
    """Assert §9: no provider/CLI/model names in type."""
    tokens = set(ce_type.split("."))
    intersect = tokens & BANNED_TOKENS
    if intersect:
        raise ContractViolation(
            f"type {ce_type!r} contains banned tokens {sorted(intersect)} (§9); "
            f"provider identity belongs in actor, not type"
        )


def assert_action_tense(action: str, kind: str) -> None:
    """Assert §5: past-tense for kind=event, imperative for kind=command, mirror for kind=reply."""
    if kind == "event":
        if action not in EVENT_ACTIONS:
            raise ContractViolation(
                f"event action {action!r} not in past-tense allowlist (§8.1); "
                f"events use past tense / past participle"
            )
        if action in {"response", "request"}:
            raise ContractViolation(
                f"action {action!r} is a noun, not a verb; use the verb pair "
                f"received/sent (§9)"
            )
    elif kind == "command":
        if action not in COMMAND_ACTIONS:
            raise ContractViolation(
                f"command action {action!r} not in imperative allowlist (§8.2); "
                f"commands use imperative present tense"
            )
    elif kind == "reply":
        # Replies mirror the command they answer.
        if action not in COMMAND_ACTIONS:
            raise ContractViolation(
                f"reply action {action!r} must mirror a command action (§5)"
            )
    else:
        raise ContractViolation(
            f"unknown kind {kind!r}; expected event|command|reply (§4)"
        )


def subject_for(ce_type: str, kind: str) -> str:
    """Build the NATS subject for (type, kind). §3.

    Type:    bloodbank.v1.<domain>.<entity>.<action>
    Subject: bloodbank.<evt|cmd|rpy>.v1.<domain>.<entity>.<action>
    """
    vendor, version, domain, entity, action = _split_type(ce_type)
    if kind not in KIND_MARKERS:
        raise ContractViolation(
            f"unknown kind {kind!r}; expected event|command|reply (§4)"
        )
    marker = KIND_MARKERS[kind]
    return f"{vendor}.{marker}.{version}.{domain}.{entity}.{action}"


def assert_subject_matches(subject: str, ce_type: str, kind: str) -> None:
    """Assert §3: subject mirrors type with the kind marker injected."""
    expected = subject_for(ce_type, kind)
    if subject != expected:
        raise ContractViolation(
            f"subject {subject!r} does not match expected {expected!r} "
            f"for type={ce_type!r} kind={kind!r}"
        )


REQUIRED_BASE_FIELDS = (
    "specversion", "id", "source", "type", "time",
    "correlationid", "producer", "service", "domain", "kind", "data",
)
REQUIRED_EVENT_FIELDS = ("actor", "ordering_key")
REQUIRED_COMMAND_FIELDS = ("actor", "command_id", "idempotency_key", "delivery")


def assert_contract(envelope: dict) -> None:
    """Run all stdlib-only contract checks on an envelope. Raises ContractViolation on first failure."""
    if not isinstance(envelope, dict):
        raise ContractViolation(f"envelope must be dict, got {type(envelope).__name__}")

    # §11 base fields
    missing = [f for f in REQUIRED_BASE_FIELDS if f not in envelope]
    if missing:
        raise ContractViolation(f"envelope missing required fields: {missing}")

    ce_type = envelope["type"]
    kind = envelope["kind"]

    # §2 + explicit post-v1 registration + §6-§9
    assert_registered_version(ce_type)
    domain, entity, action = assert_type_shape(ce_type)
    assert_banned_tokens(ce_type)
    assert_action_tense(action, kind)

    # §4 kind value
    if kind not in {"event", "command", "reply"}:
        raise ContractViolation(f"kind {kind!r} not in {{event, command, reply}} (§4)")

    # §11 kind-specific required fields
    extra_required = REQUIRED_EVENT_FIELDS if kind == "event" else REQUIRED_COMMAND_FIELDS if kind == "command" else ("actor",)
    missing_extra = [f for f in extra_required if f not in envelope]
    if missing_extra:
        raise ContractViolation(
            f"envelope missing {kind}-required fields: {missing_extra}"
        )

    # §11 domain field MUST match type segment 3
    if envelope["domain"] != domain:
        raise ContractViolation(
            f"envelope.domain {envelope['domain']!r} does not match "
            f"type segment 3 {domain!r}"
        )

    # §3 subject shape (if provided)
    subject = envelope.get("subject")
    if subject is not None:
        if not SUBJECT_REGEX.match(subject):
            raise ContractViolation(
                f"subject {subject!r} does not match versioned regex {SUBJECT_REGEX.pattern!r}"
            )
        assert_subject_matches(subject, ce_type, kind)

    # §11 actor shape
    actor = envelope["actor"]
    if not isinstance(actor, dict):
        raise ContractViolation(f"actor must be dict, got {type(actor).__name__}")
    for f in ("type", "agent_id"):
        if not actor.get(f):
            raise ContractViolation(f"actor.{f} is required (§10)")

    # §11.2 command delivery
    if kind == "command" and envelope.get("delivery") != "single_consumer":
        raise ContractViolation(
            f"command.delivery must be 'single_consumer' for v1 (§11)"
        )


# --------------------------------------------------------------------------
# Optional JSON Schema validation
# --------------------------------------------------------------------------

_RFC3339_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _is_rfc3339_datetime(value: object) -> bool:
    """Return whether value is a real RFC 3339 date-time."""
    if not isinstance(value, str) or not _RFC3339_DATETIME.fullmatch(value):
        return False
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


def _draft202012_format_checker(validator_class: Any) -> Any:
    """Use Draft 2020-12 formats, filling the optional date-time checker gap."""
    checker = validator_class.FORMAT_CHECKER
    if "date-time" not in checker.checkers:
        checker.checks("date-time")(_is_rfc3339_datetime)
    return checker


def _schemas_root() -> Path:
    """Locate the schema tree.

    Resolution order:
      1. BLOODBANK_SCHEMAS_DIR env var (new canonical override).
      2. HOLYFIELDS_SCHEMAS_DIR env var (backward-compat override).
      3. Bloodbank repo's own schemas/ — walk up from this file looking for
         a directory that contains both docs/event-naming.md and schemas/.
      4. A sibling holyfields/schemas/ — transition fallback.
      5. Hard fallback under $HOME/code/33GOD.
    """
    override = os.environ.get("BLOODBANK_SCHEMAS_DIR")
    if override:
        return Path(override)
    override = os.environ.get("HOLYFIELDS_SCHEMAS_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        local = parent / "schemas"
        marker = parent / "docs" / "event-naming.md"
        if local.is_dir() and marker.is_file():
            return local
    for parent in here.parents:
        sibling = parent.parent / "holyfields" / "schemas"
        if sibling.is_dir():
            return sibling
    home_local = Path.home() / "code" / "33GOD" / "bloodbank" / "schemas"
    if home_local.is_dir():
        return home_local
    return Path.home() / "code" / "33GOD" / "holyfields" / "schemas"


def _schema_path_for(ce_type: str) -> Path:
    """Map a CE type to its versioned schema directory and v1 schema file."""
    _, version, domain, entity, action = _split_type(ce_type)
    if version != "v1":
        relative = REGISTERED_NON_V1_SCHEMAS.get(ce_type)
        if relative is None:
            raise ContractViolation(
                f"type {ce_type!r} has no registered non-v1 schema"
            )
        return _schemas_root() / relative
    return (
        _schemas_root()
        / "bloodbank"
        / version
        / domain
        / f"{entity}.{action}.v1.json"
    )


@lru_cache(maxsize=64)
def _load_schema(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _build_registry() -> Any:
    """Pre-load every schema under _schemas_root() into a referencing.Registry.

    Schemas use absolute `$id` URLs (`https://33god.dev/schemas/...`), so the
    naive jsonschema resolver would try to fetch them from the network. The
    registry maps each `$id` to its on-disk schema, so refs resolve locally.

    Cached once per process; bust the cache by re-importing the module.
    """
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    resources: list[tuple[str, Any]] = []
    for path in sorted(_schemas_root().rglob("*.json")):
        try:
            schema = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        schema_id = schema.get("$id")
        if not schema_id:
            continue
        resources.append(
            (schema_id, Resource(contents=schema, specification=DRAFT202012))
        )
    return Registry().with_resources(resources)


def validate_envelope(envelope: dict) -> None:
    """Stdlib contract assertions + optional JSON Schema validation.

    Always runs assert_contract. If jsonschema is importable, additionally
    validates against the corresponding bloodbank/schemas schema using a
    pre-populated `referencing.Registry` so absolute-`$id` refs resolve from
    the local tree (no network fetch). Otherwise raises ValidationUnavailable.
    """
    assert_contract(envelope)

    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:  # pragma: no cover
        raise ValidationUnavailable(
            "jsonschema is not installed; set BLOODBANK_HOOK_VALIDATE=0 to skip"
        ) from exc

    schema_path = _schema_path_for(envelope["type"])
    try:
        schema = _load_schema(str(schema_path))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise EnvelopeInvalid(
            f"registered schema for {envelope['type']!r} is unavailable or invalid: "
            f"{schema_path}"
        ) from exc
    registry = _build_registry()

    validator = Draft202012Validator(
        schema,
        registry=registry,
        format_checker=_draft202012_format_checker(Draft202012Validator),
    )
    errors = sorted(validator.iter_errors(envelope), key=lambda e: e.path)
    if errors:
        first = errors[0]
        loc = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise EnvelopeInvalid(
            f"envelope failed JSON Schema validation against {schema_path.name} "
            f"at {loc}: {first.message}"
        )


def load_schema_for(ce_type: str) -> dict:
    """Read the schema for a versioned Bloodbank type as a dict."""
    return _load_schema(str(_schema_path_for(ce_type)))


__all__ = [
    "ALLOWED_DOMAINS",
    "ALLOWED_ENTITIES",
    "REGISTERED_NON_V1_SCHEMAS",
    "BANNED_TOKENS",
    "COMMAND_ACTIONS",
    "ContractViolation",
    "EVENT_ACTIONS",
    "EnvelopeInvalid",
    "KIND_MARKERS",
    "MARKER_TO_KIND",
    "SUBJECT_REGEX",
    "TYPE_REGEX",
    "ValidationUnavailable",
    "assert_action_tense",
    "assert_banned_tokens",
    "assert_contract",
    "assert_subject_matches",
    "assert_type_shape",
    "assert_registered_version",
    "load_schema_for",
    "subject_for",
    "validate_envelope",
]
