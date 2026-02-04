import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from typer.testing import CliRunner
from event_producers.cli import app
import sys

runner = CliRunner()

@pytest.fixture
def mock_deps():
    with patch("event_producers.cli.get_event_by_name") as mock_get_event, \
         patch("event_producers.cli.load_mock_data") as mock_load_data, \
         patch("event_producers.cli.httpx.post") as mock_post:

        # Setup a fake event class
        EventClass = MagicMock()
        event_instance = MagicMock()
        event_instance.model_dump.return_value = {"some": "data"}
        EventClass.return_value = event_instance

        mock_get_event.return_value = {
            "name": "TestEvent",
            "routing_key": "test.event",
            "class": EventClass,
            "domain": "test",
            "is_command": False
        }

        mock_load_data.return_value = {"some": "data"}

        mock_post.return_value.status_code = 200

        yield mock_get_event, mock_load_data, mock_post

def test_publish_rabbit_success(mock_deps):
    """Test that RabbitMQ is used if available."""
    _, _, mock_post = mock_deps

    with patch("event_producers.rabbit.Publisher") as MockPublisher:
        instance = MockPublisher.return_value
        instance.start = AsyncMock()
        instance.publish = AsyncMock()
        instance.close = AsyncMock()

        result = runner.invoke(app, ["publish", "test.event", "--mock"])

        if result.exit_code != 0:
            print(result.stdout)
            print(result.exception)

        assert result.exit_code == 0
        instance.publish.assert_awaited_once()
        mock_post.assert_not_called()

def test_publish_fallback_http(mock_deps):
    """Test that it falls back to HTTP if RabbitMQ fails."""
    _, _, mock_post = mock_deps

    with patch("event_producers.rabbit.Publisher") as MockPublisher:
        instance = MockPublisher.return_value
        # Simulate connection failure
        instance.start = AsyncMock(side_effect=Exception("Connection failed"))

        result = runner.invoke(app, ["publish", "test.event", "--mock"])

        if result.exit_code != 0:
            print(result.stdout)
            print(result.exception)

        assert result.exit_code == 0
        # Publisher was attempted
        instance.start.assert_awaited_once()
        # Fallback to HTTP
        mock_post.assert_called_once()
