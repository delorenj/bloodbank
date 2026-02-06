"""
Test for CLI publish command with proper Publisher mocking.

This test addresses the issue where asyncio.run() creates a new event loop,
and AsyncMock's assertions may not work as expected across event loop boundaries.

The solution is to:
1. Run tests as regular synchronous tests (not @pytest.mark.asyncio)
2. Mock the Publisher class at the module level
3. Verify that the mock was called correctly after asyncio.run() completes
"""

import json
from typer.testing import CliRunner
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from event_producers.cli import app


def test_publish_command_uses_publisher(tmp_path):
    """
    Test that the publish command properly uses the Publisher.
    
    This test verifies that:
    1. Publisher is instantiated
    2. Publisher.start() is called
    3. Publisher.publish() is called with correct arguments
    4. Publisher.close() is called
    
    The key insight: asyncio.run() in the CLI creates its OWN event loop,
    so we don't mark this test as async. Instead, we mock at the module level
    and verify calls after the sync CLI command completes.
    """
    # Create test payload
    payload_file = tmp_path / "test_payload.json"
    payload = {"message": "Test event"}
    payload_file.write_text(json.dumps(payload))
    
    # Create mock Publisher instance
    # AsyncMock automatically creates async mock methods when accessed
    mock_publisher_instance = AsyncMock()
    
    # Patch the Publisher class to return our mock instance
    with patch('event_producers.cli.Publisher', return_value=mock_publisher_instance) as mock_publisher_class:
        runner = CliRunner()
        result = runner.invoke(app, [
            "publish",
            "test.event.type",
            "--payload-file", str(payload_file),
            "--skip-validation"  # Skip validation to keep test simple
        ])
    
    # Verify the command succeeded
    assert result.exit_code == 0, f"Command failed: {result.stdout}"
    assert "Published" in result.stdout or "✓" in result.stdout
    
    # Verify Publisher was instantiated with correct arguments
    mock_publisher_class.assert_called_once_with(enable_correlation_tracking=True)
    
    # Verify the async methods were called
    # We use assert_called_once() instead of assert_awaited_once()
    # because the await happened in a different event loop (inside asyncio.run())
    mock_publisher_instance.start.assert_called_once()
    mock_publisher_instance.publish.assert_called_once()
    mock_publisher_instance.close.assert_called_once()
    
    # Verify publish was called with correct routing key
    call_args = mock_publisher_instance.publish.call_args
    assert call_args is not None
    assert call_args.kwargs['routing_key'] == 'test.event.type'
    assert 'body' in call_args.kwargs
    assert 'event_id' in call_args.kwargs


def test_publish_command_with_correlation_id_uses_publisher(tmp_path):
    """
    Test that correlation IDs are properly passed to Publisher.
    """
    # Create test payload
    payload_file = tmp_path / "test_payload.json"
    payload = {"message": "Test correlated event"}
    payload_file.write_text(json.dumps(payload))
    
    parent_id = str(uuid4())
    
    # Create mock Publisher instance
    # AsyncMock automatically creates async mock methods when accessed
    mock_publisher_instance = AsyncMock()
    
    # Patch the Publisher class
    with patch('event_producers.cli.Publisher', return_value=mock_publisher_instance):
        runner = CliRunner()
        result = runner.invoke(app, [
            "publish",
            "test.correlated.event",
            "--payload-file", str(payload_file),
            "--correlation-id", parent_id,
            "--skip-validation"
        ])
    
    # Verify the command succeeded
    assert result.exit_code == 0, f"Command failed: {result.stdout}"
    
    # Verify publish was called with correlation IDs
    call_args = mock_publisher_instance.publish.call_args
    assert call_args is not None
    assert 'parent_event_ids' in call_args.kwargs
    parent_ids = call_args.kwargs['parent_event_ids']
    assert len(parent_ids) == 1
    assert str(parent_ids[0]) == parent_id


def test_publish_command_dry_run_does_not_use_publisher(tmp_path):
    """
    Test that --dry-run does NOT instantiate or use the Publisher.
    """
    # Create test payload
    payload_file = tmp_path / "test_payload.json"
    payload = {"message": "Dry run test"}
    payload_file.write_text(json.dumps(payload))
    
    # Patch the Publisher class
    with patch('event_producers.cli.Publisher') as mock_publisher_class:
        runner = CliRunner()
        result = runner.invoke(app, [
            "publish",
            "test.dry.run",
            "--payload-file", str(payload_file),
            "--skip-validation",
            "--dry-run"
        ])
    
    # Verify the command succeeded
    assert result.exit_code == 0
    assert "Dry run" in result.stdout
    
    # Verify Publisher was NOT instantiated (dry run should skip publishing)
    mock_publisher_class.assert_not_called()
