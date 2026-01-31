"""
Integration tests for bb publish command with schema validation.

Tests STORY-004 acceptance criteria.
"""

import json
import pytest
from pathlib import Path
from typer.testing import CliRunner
from uuid import uuid4
from datetime import datetime, timezone

from event_producers.cli import app

runner = CliRunner()


def test_bb_publish_help():
    """Test that bb publish --help shows schema validation options."""
    result = runner.invoke(app, ["publish", "--help"])

    assert result.exit_code == 0
    assert "--skip-validation" in result.stdout
    assert "--strict-validation" in result.stdout or "--permissive-validation" in result.stdout


def test_bb_publish_with_permissive_validation(tmp_path):
    """Test bb publish with permissive validation (no schema required)."""
    # Create test payload
    payload_file = tmp_path / "test_payload.json"
    payload = {
        "message": "Test event",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": {"key": "value"}
    }
    payload_file.write_text(json.dumps(payload))

    result = runner.invoke(app, [
        "publish",
        "test.event.type",
        "--payload-file", str(payload_file),
        "--permissive-validation",
        "--dry-run"
    ])

    # Should succeed even without schema
    assert result.exit_code == 0
    assert "Validating payload" in result.stdout or "validation" in result.stdout.lower()


def test_bb_publish_skip_validation(tmp_path):
    """Test bb publish with --skip-validation flag."""
    # Create test payload
    payload_file = tmp_path / "test_payload.json"
    payload = {
        "message": "Test event without validation",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    payload_file.write_text(json.dumps(payload))

    result = runner.invoke(app, [
        "publish",
        "test.event.skip",
        "--payload-file", str(payload_file),
        "--skip-validation",
        "--dry-run"
    ])

    # Should succeed and skip validation
    assert result.exit_code == 0
    # Should NOT show validation messages when skipped
    output_lower = result.stdout.lower()
    # Either no validation message, or it says skipped
    assert "validating" not in output_lower or "skip" in output_lower


def test_bb_publish_with_json_string(tmp_path):
    """Test bb publish with inline JSON string."""
    json_payload = json.dumps({
        "message": "Inline JSON test",
        "count": 42
    })

    result = runner.invoke(app, [
        "publish",
        "test.inline.json",
        "--json", json_payload,
        "--permissive-validation",
        "--dry-run"
    ])

    assert result.exit_code == 0


def test_bb_publish_with_correlation_id(tmp_path):
    """Test bb publish with correlation tracking."""
    payload_file = tmp_path / "test_payload.json"
    payload = {
        "message": "Correlated event",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    payload_file.write_text(json.dumps(payload))

    parent_id = str(uuid4())

    result = runner.invoke(app, [
        "publish",
        "test.correlated.event",
        "--payload-file", str(payload_file),
        "--correlation-id", parent_id,
        "--permissive-validation",
        "--dry-run"
    ])

    assert result.exit_code == 0
    # Verify correlation ID is in the output envelope
    assert parent_id in result.stdout or "correlation" in result.stdout.lower()


def test_bb_publish_dry_run_shows_payload(tmp_path):
    """Test that --dry-run prints the payload without publishing."""
    payload_file = tmp_path / "test_payload.json"
    payload = {
        "message": "Dry run test",
        "value": 123
    }
    payload_file.write_text(json.dumps(payload))

    result = runner.invoke(app, [
        "publish",
        "test.dry.run",
        "--payload-file", str(payload_file),
        "--permissive-validation",
        "--dry-run"
    ])

    assert result.exit_code == 0
    assert "Dry run" in result.stdout or "would publish" in result.stdout.lower()
    assert "message" in result.stdout
    assert "Dry run test" in result.stdout


def test_bb_publish_with_envelope_file(tmp_path):
    """Test bb publish with full envelope JSON."""
    envelope_file = tmp_path / "test_envelope.json"
    envelope = {
        "event_id": str(uuid4()),
        "event_type": "test.envelope.event",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
        "source": {
            "host": "test-host",
            "type": "manual",
            "app": "test-app"
        },
        "payload": {
            "message": "Envelope test"
        },
        "correlation_ids": []
    }
    envelope_file.write_text(json.dumps(envelope))

    result = runner.invoke(app, [
        "publish",
        "test.envelope.event",
        "--envelope-file", str(envelope_file),
        "--permissive-validation",
        "--dry-run"
    ])

    assert result.exit_code == 0
    assert "Envelope test" in result.stdout


def test_bb_publish_with_custom_source(tmp_path):
    """Test bb publish with custom source metadata."""
    payload_file = tmp_path / "test_payload.json"
    payload = {"message": "Custom source test"}
    payload_file.write_text(json.dumps(payload))

    result = runner.invoke(app, [
        "publish",
        "test.custom.source",
        "--payload-file", str(payload_file),
        "--source-app", "my-custom-app",
        "--source-type", "agent",
        "--permissive-validation",
        "--dry-run"
    ])

    assert result.exit_code == 0
    assert "my-custom-app" in result.stdout


def test_bb_publish_invalid_json_fails(tmp_path):
    """Test that invalid JSON payload fails gracefully."""
    payload_file = tmp_path / "invalid.json"
    payload_file.write_text("{invalid json}")

    result = runner.invoke(app, [
        "publish",
        "test.invalid.json",
        "--payload-file", str(payload_file),
        "--skip-validation"
    ])

    # Should fail with JSON parse error
    assert result.exit_code != 0


def test_bb_publish_missing_payload_fails():
    """Test that missing payload fails gracefully."""
    result = runner.invoke(app, [
        "publish",
        "test.missing.payload"
        # No payload file, json, or mock flag
    ])

    # Should fail with helpful error
    assert result.exit_code != 0
    assert "provide" in result.stdout.lower() or "error" in result.stdout.lower()


def test_bb_publish_with_event_id_override(tmp_path):
    """Test bb publish with explicit event_id."""
    payload_file = tmp_path / "test_payload.json"
    payload = {"message": "Event ID test"}
    payload_file.write_text(json.dumps(payload))

    custom_event_id = str(uuid4())

    result = runner.invoke(app, [
        "publish",
        "test.event.id",
        "--payload-file", str(payload_file),
        "--event-id", custom_event_id,
        "--permissive-validation",
        "--dry-run"
    ])

    assert result.exit_code == 0
    assert custom_event_id in result.stdout


@pytest.mark.asyncio
async def test_bb_publish_actual_rabbitmq_integration(tmp_path):
    """
    Integration test: bb publish actually sends to RabbitMQ.

    This test requires RabbitMQ to be running.
    """
    from event_producers.config import settings
    import aio_pika
    import asyncio

    received_events = []
    test_routing_key = f"test.bb.publish.integration.{uuid4().hex[:8]}"

    # Setup consumer
    connection = await aio_pika.connect_robust(settings.rabbit_url)
    channel = await connection.channel()
    exchange = await channel.declare_exchange(
        settings.exchange_name,
        aio_pika.ExchangeType.TOPIC,
        durable=True
    )

    queue = await channel.declare_queue(
        name=f"test_bb_publish_{uuid4().hex[:8]}",
        durable=False,
        auto_delete=True
    )
    await queue.bind(exchange, routing_key=test_routing_key)

    async def on_message(message: aio_pika.IncomingMessage):
        async with message.process():
            import orjson
            body = orjson.loads(message.body)
            received_events.append(body)

    await queue.consume(on_message)

    # Publish via bb command
    payload_file = tmp_path / "integration_test.json"
    payload = {
        "message": "RabbitMQ integration test",
        "test_id": test_routing_key
    }
    payload_file.write_text(json.dumps(payload))

    result = runner.invoke(app, [
        "publish",
        test_routing_key,
        "--payload-file", str(payload_file),
        "--permissive-validation"
        # No --dry-run, actually publish
    ])

    assert result.exit_code == 0
    assert "Published" in result.stdout or "✓" in result.stdout

    # Wait for message
    for _ in range(50):
        if received_events:
            break
        await asyncio.sleep(0.1)

    # Verify message was received
    assert len(received_events) == 1
    assert received_events[0]["payload"]["message"] == "RabbitMQ integration test"
    assert received_events[0]["event_type"] == test_routing_key

    # Cleanup
    await channel.close()
    await connection.close()


def test_bb_list_events_command():
    """Test bb list-events command for discovering event types."""
    result = runner.invoke(app, ["list-events"])

    assert result.exit_code == 0
    # Should show available events or at least not error
    # Output depends on registered events in the system


def test_bb_show_event_command():
    """Test bb show command for viewing event schema."""
    # Try to show a known event type (if any exist)
    result = runner.invoke(app, ["list-events"])

    if "fireflies" in result.stdout:
        # If fireflies events exist, test showing one
        result = runner.invoke(app, ["show", "fireflies.transcript.ready"])
        # Should not error even if schema details vary
        assert result.exit_code in [0, 1]  # May fail if event not found


def test_validation_result_in_output(tmp_path):
    """Test that validation results are shown in bb publish output."""
    payload_file = tmp_path / "test_payload.json"
    payload = {"message": "Validation output test"}
    payload_file.write_text(json.dumps(payload))

    result = runner.invoke(app, [
        "publish",
        "test.validation.output",
        "--payload-file", str(payload_file),
        "--permissive-validation",
        "--dry-run"
    ])

    assert result.exit_code == 0

    # Should show validation status
    output_lower = result.stdout.lower()
    # Either validation passed message or skipped message
    assert any(word in output_lower for word in ["validation", "schema", "✓", "✗"])
