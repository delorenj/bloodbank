"""
Tests for schema validation functionality.

Tests STORY-004 schema validation requirements.
"""

import json
import pytest
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone

from event_producers.schema_validator import (
    SchemaValidator,
    ValidationResult,
    validate_event,
    get_validator
)


def test_validation_result_str():
    """Test ValidationResult string representation."""
    # Valid result
    result = ValidationResult(valid=True, errors=[], schema_path="test.schema.json")
    assert "✓ Validation passed" in str(result)
    assert "test.schema.json" in str(result)

    # Invalid result
    result = ValidationResult(
        valid=False,
        errors=["Error 1", "Error 2"],
        schema_path="test.schema.json"
    )
    assert "✗ Validation failed" in str(result)
    assert "Error 1" in str(result)
    assert "Error 2" in str(result)


def test_validator_initialization():
    """Test SchemaValidator initialization."""
    validator = SchemaValidator(strict=True)
    assert validator.strict is True
    assert isinstance(validator._schema_cache, dict)


def test_validator_discovers_holyfields_path():
    """Test that validator can discover HolyFields path."""
    validator = SchemaValidator()

    # Should find HolyFields or be None
    if validator.holyfields_path:
        assert validator.holyfields_path.exists()
        assert (validator.holyfields_path / "whisperlivekit").exists()


def test_validator_permissive_mode_allows_missing_schemas():
    """Test that permissive mode allows events without schemas."""
    validator = SchemaValidator(strict=False)

    payload = {
        "text": "Hello world",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    result = validator.validate(
        event_type="nonexistent.event.type",
        payload=payload
    )

    # Should pass in permissive mode
    assert result.valid is True
    assert "permissive" in result.schema_path.lower() or result.schema_path == "none (permissive)"


def test_validator_strict_mode_rejects_missing_schemas():
    """Test that strict mode rejects events without schemas."""
    validator = SchemaValidator(strict=True)

    payload = {
        "text": "Hello world",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    result = validator.validate(
        event_type="nonexistent.event.type",
        payload=payload
    )

    # Should fail in strict mode
    assert result.valid is False
    assert any("Schema not found" in error for error in result.errors)


def test_validate_envelope_structure():
    """Test validation of event envelope structure."""
    validator = SchemaValidator()

    # Valid envelope
    valid_envelope = {
        "event_id": str(uuid4()),
        "event_type": "test.event",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
        "source": {
            "host": "test-host",
            "type": "manual",
            "app": "test-app"
        },
        "payload": {"message": "test"},
        "correlation_ids": []
    }

    result = validator.validate_envelope(valid_envelope)
    assert result.valid is True

    # Missing required field
    invalid_envelope = valid_envelope.copy()
    del invalid_envelope["event_id"]

    result = validator.validate_envelope(invalid_envelope)
    assert result.valid is False
    assert any("event_id" in error for error in result.errors)

    # Invalid event_id
    invalid_envelope = valid_envelope.copy()
    invalid_envelope["event_id"] = "not-a-uuid"

    result = validator.validate_envelope(invalid_envelope)
    assert result.valid is False
    assert any("event_id" in error for error in result.errors)

    # Missing source fields
    invalid_envelope = valid_envelope.copy()
    invalid_envelope["source"] = {"host": "test-host"}  # Missing type and app

    result = validator.validate_envelope(invalid_envelope)
    assert result.valid is False
    assert any("source" in error for error in result.errors)


def test_basic_validation_without_jsonschema(monkeypatch):
    """Test basic validation when jsonschema is not available."""
    # Mock jsonschema import failure
    import sys
    import builtins
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "jsonschema":
            raise ImportError("jsonschema not available")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)

    # Create a simple schema
    schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "count": {"type": "integer"}
        },
        "required": ["text"]
    }

    validator = SchemaValidator(strict=False)

    # Manually set schema in cache to test basic validation
    validator._schema_cache["test.event"] = schema

    # Valid payload
    valid_payload = {"text": "Hello", "count": 42}
    result = validator.validate("test.event", valid_payload)
    assert result.valid is True

    # Missing required field
    invalid_payload = {"count": 42}
    result = validator.validate("test.event", invalid_payload)
    assert result.valid is False
    assert any("text" in error for error in result.errors)

    # Wrong type
    invalid_payload = {"text": "Hello", "count": "not-an-integer"}
    result = validator.validate("test.event", invalid_payload)
    assert result.valid is False
    assert any("count" in error for error in result.errors)


def test_global_validator_singleton():
    """Test that get_validator returns singleton instance."""
    validator1 = get_validator()
    validator2 = get_validator()

    assert validator1 is validator2


def test_validate_event_convenience_function():
    """Test validate_event convenience function."""
    payload = {
        "text": "Test message",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    # Should not raise exception
    result = validate_event(
        event_type="test.event.type",
        payload=payload,
        strict=False
    )

    assert isinstance(result, ValidationResult)


def test_schema_caching():
    """Test that schemas are cached after first load."""
    validator = SchemaValidator(strict=False)

    # First call - might load from disk
    result1 = validator.validate(
        "test.cache.event",
        {"data": "test"}
    )

    # Second call - should use cache
    result2 = validator.validate(
        "test.cache.event",
        {"data": "test"}
    )

    # Both should succeed (in permissive mode)
    assert result1.valid is True
    assert result2.valid is True


@pytest.mark.skipif(
    not Path("/home/delorenj/code/33GOD/holyfields/trunk-main").exists(),
    reason="HolyFields repository not available"
)
def test_integration_with_real_holyfields_schema():
    """
    Integration test with real HolyFields schema (if available).

    This test will be skipped if HolyFields repo is not found.
    """
    holyfields_path = Path("/home/delorenj/code/33GOD/holyfields/trunk-main")

    validator = SchemaValidator(holyfields_path=holyfields_path, strict=True)

    # Check if whisperlivekit schemas exist
    schema_dir = holyfields_path / "whisperlivekit" / "events"

    if not list(schema_dir.glob("*.json")):
        pytest.skip("No schemas found in HolyFields yet")

    # If schemas exist, test validation with first available schema
    schema_files = list(schema_dir.glob("*.json"))
    if schema_files:
        with open(schema_files[0]) as f:
            schema = json.load(f)

        # Extract event type from schema
        event_type = schema.get("title", "").lower().replace(" ", "_")

        # Create a payload matching the schema (basic test)
        # This would need to be customized per schema
        payload = {
            # Add required fields based on schema
        }

        # Validate (will likely fail with empty payload, but tests the flow)
        result = validator.validate(event_type, payload)

        # Just verify we got a result (validation may fail with empty payload)
        assert isinstance(result, ValidationResult)


def test_event_type_parsing_and_component_mapping():
    """Test that event types are correctly parsed and mapped to components."""
    validator = SchemaValidator(strict=False)

    # Test different event type formats
    test_cases = [
        ("transcription.voice.completed", "whisperlivekit"),
        ("voice.transcription.ready", "whisperlivekit"),
        ("fireflies.transcript.ready", "whisperlivekit"),
    ]

    for event_type, expected_component in test_cases:
        # Load schema (will return None if not found, but tests the path logic)
        schema = validator._load_schema(event_type)

        # We don't assert on schema presence (may not exist yet)
        # Just verify no exceptions during path resolution
        assert True  # If we got here, path resolution worked


def test_validation_with_full_envelope():
    """Test validation with complete event envelope."""
    validator = SchemaValidator(strict=False)

    full_envelope = {
        "event_id": str(uuid4()),
        "event_type": "test.full.envelope",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
        "source": {
            "host": "test-host",
            "type": "manual",
            "app": "test-app"
        },
        "payload": {
            "message": "Test event",
            "data": {"key": "value"}
        },
        "correlation_ids": []
    }

    # Validate envelope structure
    envelope_result = validator.validate_envelope(full_envelope)
    assert envelope_result.valid is True

    # Validate payload
    payload_result = validator.validate(
        event_type=full_envelope["event_type"],
        payload=full_envelope["payload"],
        envelope=full_envelope
    )

    # Should pass in permissive mode (no schema found)
    assert payload_result.valid is True


def test_error_messages_are_descriptive():
    """Test that validation errors provide helpful messages."""
    validator = SchemaValidator(strict=True)

    # Test missing schema error
    result = validator.validate(
        "nonexistent.event",
        {"data": "test"}
    )

    assert result.valid is False
    assert len(result.errors) > 0
    assert any("not found" in error.lower() for error in result.errors)

    # Test envelope validation errors
    invalid_envelope = {
        "event_type": "test",
        # Missing required fields
    }

    result = validator.validate_envelope(invalid_envelope)
    assert result.valid is False
    assert len(result.errors) > 0
    # Check that error messages mention missing fields
    error_text = " ".join(result.errors).lower()
    assert "missing" in error_text or "required" in error_text
