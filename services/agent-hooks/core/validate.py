"""Optional JSON Schema validation against Holyfields schemas.

Off by default at hook time — set BLOODBANK_HOOK_VALIDATE=1 (or pass
validate=True to build_envelope) to opt in. Used unconditionally in CI.

Requires ``jsonschema`` to be importable. If it's not, raises
ValidationUnavailable so callers can fail loudly (validation requested but
not possible) instead of silently skipping.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


class ValidationUnavailable(RuntimeError):
    """Raised when validation is requested but jsonschema isn't installed."""


class EnvelopeInvalid(ValueError):
    """Raised when an envelope fails JSON Schema validation."""


def _schemas_root() -> Path:
    """Locate holyfields/schemas/.

    Search order:
      1. ``HOLYFIELDS_SCHEMAS_DIR`` env var (explicit override).
      2. Walk up from this file looking for a ``holyfields/schemas`` dir
         (works when the repo is checked out alongside bloodbank in 33GOD/).
      3. ``~/code/33GOD/holyfields/schemas`` (last-resort default for this
         developer environment).
    """
    override = os.environ.get("HOLYFIELDS_SCHEMAS_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent.parent / "holyfields" / "schemas"
        if candidate.is_dir():
            return candidate
    return Path.home() / "code" / "33GOD" / "holyfields" / "schemas"


def _schema_path_for(ce_type: str) -> Path:
    """Map a CloudEvents type like ``copilot.tool.post`` to its schema file."""
    if "." not in ce_type:
        raise EnvelopeInvalid(f"ce_type {ce_type!r} has no domain segment")
    domain, _, rest = ce_type.partition(".")
    return _schemas_root() / domain / f"{rest}.v1.json"


@lru_cache(maxsize=64)
def _load_schema(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def validate_envelope(envelope: dict) -> None:
    """Validate envelope against its holyfields/schemas/<domain>/<name>.v1.json.

    Raises EnvelopeInvalid on schema mismatch, ValidationUnavailable if
    jsonschema isn't importable, or FileNotFoundError if no schema exists for
    the envelope's type.
    """
    try:
        import jsonschema  # type: ignore
        from jsonschema.exceptions import RefResolutionError  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised at install time
        raise ValidationUnavailable(
            "jsonschema is not installed; install it or unset BLOODBANK_HOOK_VALIDATE"
        ) from exc

    ce_type = envelope.get("type")
    if not isinstance(ce_type, str):
        raise EnvelopeInvalid("envelope missing string 'type' field")

    schema_path = _schema_path_for(ce_type)
    schema = _load_schema(str(schema_path))

    # Resolve sibling $ref'd schemas (cloudevent_base, types) relative to
    # the schema file's directory.
    base_uri = schema_path.parent.as_uri() + "/"
    try:
        resolver = jsonschema.RefResolver(base_uri=base_uri, referrer=schema)
    except AttributeError:  # jsonschema >= 4.18 deprecates RefResolver
        resolver = None

    try:
        if resolver is not None:
            jsonschema.validate(envelope, schema, resolver=resolver)
        else:
            jsonschema.validate(envelope, schema)
    except jsonschema.ValidationError as exc:
        raise EnvelopeInvalid(
            f"envelope failed validation against {schema_path.name}: {exc.message}"
        ) from exc


def load_schema_for(ce_type: str) -> dict:
    """Convenience: read the schema for a given CE type as a dict."""
    return _load_schema(str(_schema_path_for(ce_type)))


__all__ = [
    "EnvelopeInvalid",
    "ValidationUnavailable",
    "load_schema_for",
    "validate_envelope",
]
