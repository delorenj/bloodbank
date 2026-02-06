"""
Tests for RabbitMQ publish timeout handling in CLI.

These tests validate that the CLI properly handles timeout scenarios
when RabbitMQ connections hang or become unresponsive.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from uuid import uuid4
from datetime import datetime, timezone
from typer.testing import CliRunner

from event_producers.cli import app, _publish_envelope
from event_producers.events import EventEnvelope, Source, TriggerType
from event_producers.config import settings

runner = CliRunner()


@pytest.fixture
def sample_envelope():
    """Create a sample event envelope for testing."""
    return EventEnvelope(
        event_id=uuid4(),
        event_type="test.timeout.event",
        timestamp=datetime.now(timezone.utc),
        version="1.0.0",
        source=Source(
            host="test-host",
            type=TriggerType.MANUAL,
            app="test-app"
        ),
        correlation_ids=[],
        agent_context=None,
        payload={"test": "data"}
    )


@pytest.mark.asyncio
async def test_publish_envelope_timeout(sample_envelope):
    """Test that _publish_envelope raises TimeoutError when operation exceeds timeout."""
    # Create a mock publisher that hangs during start()
    mock_publisher = AsyncMock()
    
    async def hanging_start():
        # Simulate a hanging connection by sleeping longer than timeout
        await asyncio.sleep(100)
    
    mock_publisher.start = hanging_start
    mock_publisher.close = AsyncMock()
    
    # Temporarily set a very short timeout for testing
    original_timeout = settings.rabbit_publish_timeout
    settings.rabbit_publish_timeout = 0.1
    
    try:
        with patch('event_producers.cli.Publisher', return_value=mock_publisher):
            with pytest.raises(asyncio.TimeoutError) as exc_info:
                await _publish_envelope("test.routing.key", sample_envelope)
            
            # Verify the error message is informative
            assert "timed out after" in str(exc_info.value)
            assert "0.1 seconds" in str(exc_info.value)
            
            # Verify cleanup was called
            mock_publisher.close.assert_called_once()
    finally:
        # Restore original timeout
        settings.rabbit_publish_timeout = original_timeout


@pytest.mark.asyncio
async def test_publish_envelope_timeout_during_publish(sample_envelope):
    """Test timeout during the publish operation itself."""
    mock_publisher = AsyncMock()
    mock_publisher.start = AsyncMock()
    
    async def hanging_publish(*args, **kwargs):
        # Simulate a hanging publish operation
        await asyncio.sleep(100)
    
    mock_publisher.publish = hanging_publish
    mock_publisher.close = AsyncMock()
    
    # Set a short timeout for testing
    original_timeout = settings.rabbit_publish_timeout
    settings.rabbit_publish_timeout = 0.1
    
    try:
        with patch('event_producers.cli.Publisher', return_value=mock_publisher):
            with pytest.raises(asyncio.TimeoutError):
                await _publish_envelope("test.routing.key", sample_envelope)
            
            # Verify cleanup was called
            mock_publisher.close.assert_called_once()
    finally:
        settings.rabbit_publish_timeout = original_timeout


@pytest.mark.asyncio
async def test_publish_envelope_success_within_timeout(sample_envelope):
    """Test that publish succeeds when operation completes within timeout."""
    mock_publisher = AsyncMock()
    mock_publisher.start = AsyncMock()
    mock_publisher.publish = AsyncMock()
    mock_publisher.close = AsyncMock()
    
    # Set a reasonable timeout
    original_timeout = settings.rabbit_publish_timeout
    settings.rabbit_publish_timeout = 5.0
    
    try:
        with patch('event_producers.cli.Publisher', return_value=mock_publisher):
            # Should not raise any exception
            await _publish_envelope("test.routing.key", sample_envelope)
            
            # Verify all operations were called
            mock_publisher.start.assert_called_once()
            mock_publisher.publish.assert_called_once()
            mock_publisher.close.assert_called_once()
    finally:
        settings.rabbit_publish_timeout = original_timeout


@pytest.mark.asyncio
async def test_publish_envelope_cleanup_on_error(sample_envelope):
    """Test that publisher cleanup happens even when non-timeout errors occur."""
    mock_publisher = AsyncMock()
    mock_publisher.start = AsyncMock()
    mock_publisher.publish = AsyncMock(side_effect=RuntimeError("Connection failed"))
    mock_publisher.close = AsyncMock()
    
    with patch('event_producers.cli.Publisher', return_value=mock_publisher):
        with pytest.raises(RuntimeError, match="Connection failed"):
            await _publish_envelope("test.routing.key", sample_envelope)
        
        # Verify cleanup was called even on error
        mock_publisher.close.assert_called_once()


def test_publish_command_timeout_error_handling(tmp_path):
    """Test that the CLI properly handles and displays timeout errors."""
    import json
    
    # Create a test payload file
    payload_file = tmp_path / "test_payload.json"
    payload = {"message": "Test timeout handling"}
    payload_file.write_text(json.dumps(payload))
    
    # Mock the _publish_envelope to raise TimeoutError
    async def mock_timeout_publish(*args, **kwargs):
        raise asyncio.TimeoutError(
            "RabbitMQ publish operation timed out after 30.0 seconds. "
            "Check RabbitMQ connectivity and consider increasing RABBIT_PUBLISH_TIMEOUT."
        )
    
    with patch('event_producers.cli._publish_envelope', side_effect=mock_timeout_publish):
        result = runner.invoke(app, [
            "publish",
            "test.timeout.event",
            "--payload-file", str(payload_file),
            "--skip-validation"
        ])
        
        # Should exit with error code
        assert result.exit_code == 1
        
        # Should display timeout-specific error message
        assert "Timeout error" in result.stdout or "timed out" in result.stdout.lower()
        assert "RABBIT_PUBLISH_TIMEOUT" in result.stdout or "connectivity" in result.stdout.lower()


def test_publish_command_generic_error_handling(tmp_path):
    """Test that the CLI handles non-timeout errors appropriately."""
    import json
    
    payload_file = tmp_path / "test_payload.json"
    payload = {"message": "Test generic error"}
    payload_file.write_text(json.dumps(payload))
    
    async def mock_generic_error(*args, **kwargs):
        raise RuntimeError("Generic connection error")
    
    with patch('event_producers.cli._publish_envelope', side_effect=mock_generic_error):
        result = runner.invoke(app, [
            "publish",
            "test.error.event",
            "--payload-file", str(payload_file),
            "--skip-validation"
        ])
        
        assert result.exit_code == 1
        assert "Error publishing event" in result.stdout or "error" in result.stdout.lower()


def test_timeout_setting_is_configurable():
    """Test that the timeout setting can be configured via environment."""
    # This test validates that the setting exists and has a sensible default
    assert hasattr(settings, 'rabbit_publish_timeout')
    assert isinstance(settings.rabbit_publish_timeout, (int, float))
    assert settings.rabbit_publish_timeout > 0
    # Default should be 30 seconds
    assert settings.rabbit_publish_timeout == 30.0


@pytest.mark.asyncio
async def test_publish_envelope_respects_custom_timeout(sample_envelope):
    """Test that custom timeout values are respected."""
    mock_publisher = AsyncMock()
    
    async def slow_operation():
        await asyncio.sleep(0.5)
    
    mock_publisher.start = slow_operation
    mock_publisher.close = AsyncMock()
    
    # Set timeout shorter than operation time
    original_timeout = settings.rabbit_publish_timeout
    settings.rabbit_publish_timeout = 0.1
    
    try:
        with patch('event_producers.cli.Publisher', return_value=mock_publisher):
            with pytest.raises(asyncio.TimeoutError):
                await _publish_envelope("test.routing.key", sample_envelope)
    finally:
        settings.rabbit_publish_timeout = original_timeout
    
    # Now test with longer timeout - should succeed
    mock_publisher.start = AsyncMock()
    mock_publisher.publish = AsyncMock()
    settings.rabbit_publish_timeout = 2.0
    
    try:
        with patch('event_producers.cli.Publisher', return_value=mock_publisher):
            await _publish_envelope("test.routing.key", sample_envelope)
            mock_publisher.start.assert_called_once()
    finally:
        settings.rabbit_publish_timeout = original_timeout
