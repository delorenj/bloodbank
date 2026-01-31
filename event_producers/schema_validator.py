"""
Schema validation for Bloodbank events using HolyFields schemas.

This module provides validation functionality for event payloads against
JSON schemas defined in the HolyFields repository.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of schema validation."""

    valid: bool
    errors: List[str]
    schema_path: Optional[str] = None

    def __str__(self) -> str:
        if self.valid:
            return f"✓ Validation passed (schema: {self.schema_path})"
        else:
            errors_str = "\n  ".join(self.errors)
            return f"✗ Validation failed:\n  {errors_str}"


class SchemaValidator:
    """
    Validates event payloads against HolyFields JSON schemas.

    Supports both local HolyFields repository and mock validation for testing.
    """

    def __init__(self, holyfields_path: Optional[Path] = None, strict: bool = True):
        """
        Initialize schema validator.

        Args:
            holyfields_path: Path to HolyFields repository (default: auto-discover)
            strict: If True, fail validation when schema not found
        """
        self.strict = strict
        self.holyfields_path = holyfields_path or self._discover_holyfields_path()
        self._schema_cache: Dict[str, Dict[str, Any]] = {}

        if self.holyfields_path:
            logger.info(f"Schema validator initialized with HolyFields at: {self.holyfields_path}")
        else:
            logger.warning("HolyFields repository not found - validation will be permissive")

    def _discover_holyfields_path(self) -> Optional[Path]:
        """Auto-discover HolyFields repository path."""
        # Try common locations relative to bloodbank
        possible_paths = [
            Path(__file__).parent.parent.parent / "holyfields" / "trunk-main",
            Path.home() / "code" / "33GOD" / "holyfields" / "trunk-main",
            Path("/home/delorenj/code/33GOD/holyfields/trunk-main"),
        ]

        for path in possible_paths:
            if path.exists() and (path / "whisperlivekit").exists():
                return path

        return None

    def _load_schema(self, event_type: str) -> Optional[Dict[str, Any]]:
        """
        Load JSON schema for event type.

        Args:
            event_type: Event type (e.g., "transcription.voice.completed")

        Returns:
            Parsed JSON schema or None if not found
        """
        if event_type in self._schema_cache:
            return self._schema_cache[event_type]

        if not self.holyfields_path:
            return None

        # Parse event type: <component>.<entity>.<action>
        parts = event_type.split(".")
        if len(parts) < 3:
            logger.warning(f"Invalid event type format: {event_type}")
            return None

        component = parts[0]  # e.g., "transcription" or "whisperlivekit"
        entity = parts[1]  # e.g., "voice"
        action = parts[2]  # e.g., "completed"

        # Map to HolyFields component directory
        component_map = {
            "transcription": "whisperlivekit",
            "voice": "whisperlivekit",
            "fireflies": "whisperlivekit",
        }
        holyfields_component = component_map.get(component, component)

        # Try to find schema file
        possible_paths = [
            # events/<event_type>.v1.schema.json
            self.holyfields_path / holyfields_component / "events" / f"{event_type}.v1.schema.json",
            # events/<entity>_<action>.v1.schema.json
            self.holyfields_path / holyfields_component / "events" / f"{entity}_{action}.v1.schema.json",
            # events/<action>.v1.schema.json
            self.holyfields_path / holyfields_component / "events" / f"{action}.v1.schema.json",
        ]

        for schema_path in possible_paths:
            if schema_path.exists():
                try:
                    with open(schema_path, "r") as f:
                        schema = json.load(f)
                        self._schema_cache[event_type] = schema
                        logger.info(f"Loaded schema for {event_type} from {schema_path}")
                        return schema
                except Exception as e:
                    logger.error(f"Error loading schema from {schema_path}: {e}")

        logger.warning(f"Schema not found for event type: {event_type}")
        return None

    def validate(
        self, event_type: str, payload: Dict[str, Any], envelope: Optional[Dict[str, Any]] = None
    ) -> ValidationResult:
        """
        Validate event payload against schema.

        Args:
            event_type: Event type (e.g., "transcription.voice.completed")
            payload: Event payload to validate
            envelope: Optional full envelope (for additional validation)

        Returns:
            ValidationResult with validation status and errors
        """
        errors: List[str] = []

        # Load schema
        schema = self._load_schema(event_type)

        if schema is None:
            if self.strict:
                errors.append(f"Schema not found for event type: {event_type}")
                return ValidationResult(valid=False, errors=errors)
            else:
                logger.info(f"Schema not found for {event_type}, permissive mode allows publish")
                return ValidationResult(valid=True, errors=[], schema_path="none (permissive)")

        # Validate using jsonschema if available
        try:
            import jsonschema

            try:
                jsonschema.validate(instance=payload, schema=schema)
                return ValidationResult(
                    valid=True, errors=[], schema_path=str(schema.get("$id", "unknown"))
                )
            except jsonschema.ValidationError as e:
                errors.append(f"Schema validation failed: {e.message}")
                if e.path:
                    errors.append(f"  At path: {'.'.join(str(p) for p in e.path)}")
                return ValidationResult(valid=False, errors=errors, schema_path=str(schema.get("$id")))
            except jsonschema.SchemaError as e:
                errors.append(f"Invalid schema: {e.message}")
                return ValidationResult(valid=False, errors=errors)

        except ImportError:
            # jsonschema not available, do basic validation
            logger.warning("jsonschema library not available, performing basic validation")
            return self._basic_validation(event_type, payload, schema)

    def _basic_validation(
        self, event_type: str, payload: Dict[str, Any], schema: Dict[str, Any]
    ) -> ValidationResult:
        """
        Basic validation without jsonschema library.

        Checks required fields and basic types.
        """
        errors: List[str] = []

        # Check required fields
        required = schema.get("required", [])
        for field in required:
            if field not in payload:
                errors.append(f"Missing required field: {field}")

        # Check properties types (basic)
        properties = schema.get("properties", {})
        for field, field_schema in properties.items():
            if field not in payload:
                continue

            expected_type = field_schema.get("type")
            if not expected_type:
                continue

            value = payload[field]
            type_map = {
                "string": str,
                "number": (int, float),
                "integer": int,
                "boolean": bool,
                "array": list,
                "object": dict,
            }

            expected_python_type = type_map.get(expected_type)
            if expected_python_type and not isinstance(value, expected_python_type):
                errors.append(
                    f"Field '{field}' has wrong type: expected {expected_type}, got {type(value).__name__}"
                )

        if errors:
            return ValidationResult(valid=False, errors=errors, schema_path=str(schema.get("$id")))
        else:
            return ValidationResult(
                valid=True, errors=[], schema_path=str(schema.get("$id", "unknown"))
            )

    def validate_envelope(self, envelope: Dict[str, Any]) -> ValidationResult:
        """
        Validate full event envelope.

        Args:
            envelope: Full event envelope including metadata

        Returns:
            ValidationResult for envelope structure
        """
        errors: List[str] = []

        # Check required envelope fields
        required_fields = ["event_id", "event_type", "timestamp", "version", "source", "payload"]
        for field in required_fields:
            if field not in envelope:
                errors.append(f"Missing required envelope field: {field}")

        # Validate event_id is UUID
        if "event_id" in envelope:
            try:
                from uuid import UUID

                UUID(str(envelope["event_id"]))
            except (ValueError, AttributeError):
                errors.append(f"Invalid event_id: must be UUID, got {envelope.get('event_id')}")

        # Validate source structure
        if "source" in envelope and isinstance(envelope["source"], dict):
            source = envelope["source"]
            required_source_fields = ["host", "type", "app"]
            for field in required_source_fields:
                if field not in source:
                    errors.append(f"Missing required source field: {field}")

        if errors:
            return ValidationResult(valid=False, errors=errors, schema_path="envelope")
        else:
            return ValidationResult(valid=True, errors=[], schema_path="envelope")


# Global validator instance (lazy-initialized)
_global_validator: Optional[SchemaValidator] = None


def get_validator(strict: bool = True) -> SchemaValidator:
    """
    Get global schema validator instance.

    Args:
        strict: If True, fail validation when schema not found

    Returns:
        SchemaValidator instance
    """
    global _global_validator
    if _global_validator is None:
        _global_validator = SchemaValidator(strict=strict)
    return _global_validator


def validate_event(
    event_type: str, payload: Dict[str, Any], envelope: Optional[Dict[str, Any]] = None, strict: bool = True
) -> ValidationResult:
    """
    Convenience function to validate event payload.

    Args:
        event_type: Event type (e.g., "transcription.voice.completed")
        payload: Event payload to validate
        envelope: Optional full envelope
        strict: If True, fail validation when schema not found

    Returns:
        ValidationResult with validation status
    """
    validator = get_validator(strict=strict)
    return validator.validate(event_type, payload, envelope)
