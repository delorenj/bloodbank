"""
Shared pytest configuration and fixtures for bloodbank test suite.

This file contains common fixtures and configuration that are automatically
discovered by pytest and made available to all test modules.
"""

import pytest
import asyncio
from typing import Generator
import logging

# Configure logging for tests
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


@pytest.fixture(scope="session")
def event_loop_policy():
    """Set the event loop policy for the test session."""
    return asyncio.get_event_loop_policy()


@pytest.fixture(scope="function")
async def clean_redis(fake_redis):
    """Ensure Redis is clean before each test."""
    await fake_redis.flushall()
    yield fake_redis
    await fake_redis.flushall()


@pytest.fixture(autouse=True)
def reset_logging():
    """Reset logging configuration before each test."""
    # Clear all handlers from root logger
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Reconfigure with basic config
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    yield

    # Cleanup after test
    for handler in root.handlers[:]:
        root.removeHandler(handler)


@pytest.fixture(scope="session")
def test_config():
    """Provide test configuration."""
    return {
        "redis_host": "localhost",
        "redis_port": 6379,
        "redis_db": 0,
        "rabbit_url": "amqp://guest:guest@localhost:5672/",
        "exchange_name": "bloodbank.events.test",
        "correlation_ttl_days": 30,
    }


def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
    config.addinivalue_line("markers", "unit: marks tests as unit tests")
    config.addinivalue_line("markers", "asyncio: marks tests as async tests")


def pytest_collection_modifyitems(config, items):
    """Automatically mark async tests and add other markers."""
    for item in items:
        # Automatically mark async tests
        if asyncio.iscoroutinefunction(item.function):
            item.add_marker(pytest.mark.asyncio)

        # Mark tests based on name patterns
        if "integration" in item.nodeid.lower():
            item.add_marker(pytest.mark.integration)
        elif "test_" in item.name:
            item.add_marker(pytest.mark.unit)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Make test results available to fixtures."""
    outcome = yield
    rep = outcome.get_result()

    # Add report to item for access in fixtures
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture(autouse=True)
def log_test_info(request):
    """Log test information for debugging."""
    logger = logging.getLogger("test")
    logger.info(f"Starting test: {request.node.nodeid}")

    yield

    logger.info(f"Finished test: {request.node.nodeid}")


# Pytest asyncio configuration
pytest_plugins = ["pytest_asyncio"]
