# Bloodbank Event Publisher v2.0 - Testing Guide

Comprehensive guide for testing the bloodbank event publisher with correlation tracking.

## Table of Contents
1. [Quick Start](#quick-start)
2. [Test Architecture](#test-architecture)
3. [Running Tests](#running-tests)
4. [Writing Tests](#writing-tests)
5. [Test Coverage](#test-coverage)
6. [CI/CD Integration](#cicd-integration)
7. [Troubleshooting](#troubleshooting)

## Quick Start

```bash
# Install dependencies
pip install -r tests/requirements-test.txt

# Run all tests
pytest tests/

# Run with coverage
pytest tests/ --cov=. --cov-report=html

# View coverage report
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

## Test Architecture

### Design Principles

1. **No External Dependencies**: Tests use `fakeredis` and `AsyncMock` to avoid requiring actual Redis/RabbitMQ instances
2. **Fast Execution**: All tests run in memory with no network I/O
3. **Complete Isolation**: Each test is fully isolated and can run in parallel
4. **Comprehensive Coverage**: Tests cover all features, edge cases, and error paths

### Test Structure

```
tests/
├── __init__.py                     # Package initialization
├── conftest.py                     # Shared pytest configuration and fixtures
├── test_correlation_tracking.py   # Comprehensive integration tests
├── requirements-test.txt           # Test dependencies
├── README.md                       # Test documentation
└── TESTING_GUIDE.md               # This file
```

### Fixture Hierarchy

```python
# Session-scoped (shared across all tests)
event_loop_policy
test_config

# Function-scoped (new instance per test)
fake_redis                    # In-memory Redis using fakeredis
correlation_tracker           # CorrelationTracker with fake Redis
mock_rabbitmq                 # Mocked RabbitMQ connection
publisher_with_tracking       # Publisher with correlation enabled
publisher_without_tracking    # Publisher without correlation
event_factory                 # Factory for creating test events
```

## Running Tests

### Basic Commands

```bash
# Run all tests
pytest tests/

# Run with verbose output
pytest tests/ -v

# Run with extra verbose output
pytest tests/ -vv

# Run specific test file
pytest tests/test_correlation_tracking.py

# Run specific test class
pytest tests/test_correlation_tracking.py::TestCorrelationTrackerInitialization

# Run specific test method
pytest tests/test_correlation_tracking.py::TestDeterministicEventIDGeneration::test_same_inputs_produce_same_uuid
```

### Coverage Reports

```bash
# Generate HTML coverage report
pytest tests/ --cov=. --cov-report=html
open htmlcov/index.html

# Generate terminal coverage report
pytest tests/ --cov=. --cov-report=term-missing

# Generate XML coverage report (for CI)
pytest tests/ --cov=. --cov-report=xml

# Fail if coverage below threshold
pytest tests/ --cov=. --cov-fail-under=90
```

### Filtering Tests

```bash
# Run only async tests
pytest tests/ -m asyncio

# Run only integration tests
pytest tests/ -m integration

# Run only unit tests
pytest tests/ -m unit

# Skip slow tests
pytest tests/ -m "not slow"

# Run tests matching pattern
pytest tests/ -k "test_correlation"
```

### Debugging Tests

```bash
# Show print statements
pytest tests/ -s

# Drop into pdb on failure
pytest tests/ --pdb

# Show detailed traceback
pytest tests/ --tb=long

# Show local variables in traceback
pytest tests/ -l

# Enable debug logging
pytest tests/ --log-cli-level=DEBUG

# Stop on first failure
pytest tests/ -x

# Show slowest tests
pytest tests/ --durations=10
```

### Using Makefile

```bash
# See all available targets
make help

# Run tests with coverage
make test

# Run tests and open coverage report
make test-cov

# Run tests without coverage (fast)
make test-quick

# Run tests in parallel
make test-parallel

# Run only integration tests
make test-integration

# Run only unit tests
make test-unit

# Re-run only failed tests
make test-failed

# Lint code
make lint

# Format code
make format

# Run all quality checks
make check

# Clean generated files
make clean
```

## Writing Tests

### Test Template

```python
import pytest
from uuid import uuid4

class TestFeatureName:
    """Test description of the feature."""

    @pytest.mark.asyncio
    async def test_specific_scenario(self, correlation_tracker, event_factory):
        """Test that specific scenario works correctly."""
        # Arrange: Set up test data
        parent_id = uuid4()
        child_id = uuid4()

        # Act: Perform the operation
        await correlation_tracker.add_correlation(
            child_event_id=child_id,
            parent_event_ids=[parent_id]
        )

        # Assert: Verify the result
        parents = await correlation_tracker.get_parents(child_id)
        assert len(parents) == 1
        assert parents[0] == parent_id
```

### Using Fixtures

```python
@pytest.mark.asyncio
async def test_with_event_factory(event_factory):
    """Test using event factory for test data."""
    # Create test source
    source = event_factory.create_source(
        component="test-component",
        host_id="test-host"
    )

    # Create test payload
    payload = event_factory.create_fireflies_payload(
        meeting_id="meeting-123",
        title="Test Meeting"
    )

    # Create complete envelope
    envelope = event_factory.create_event_envelope(
        event_type="test.event",
        payload=payload
    )

    assert envelope.event_type == "test.event"
    assert envelope.payload.id == "meeting-123"
```

### Async Testing

```python
import pytest

@pytest.mark.asyncio
async def test_async_operation(correlation_tracker):
    """All async tests must have @pytest.mark.asyncio decorator."""
    parent_id = uuid4()
    child_id = uuid4()

    # Use await for async operations
    await correlation_tracker.add_correlation(child_id, [parent_id])

    # Use await for async assertions
    parents = await correlation_tracker.get_parents(child_id)
    assert parents == [parent_id]
```

### Mocking External Dependencies

```python
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_with_mocked_redis():
    """Test with mocked Redis connection."""
    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True

    with patch("correlation_tracker.redis.from_url", return_value=mock_redis):
        tracker = CorrelationTracker()
        await tracker.start()

        assert tracker._started is True
        mock_redis.ping.assert_called_once()
```

### Testing Error Cases

```python
@pytest.mark.asyncio
async def test_error_handling():
    """Test graceful error handling."""
    tracker = CorrelationTracker(redis_host="nonexistent", connection_timeout=0.1)

    # Mock to raise error
    async def mock_from_url(*args, **kwargs):
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=ConnectionError("Failed"))
        return mock_redis

    with patch("correlation_tracker.redis.from_url", side_effect=mock_from_url):
        await tracker.start()

    # Should gracefully degrade
    assert tracker._started is False
    assert tracker.redis is None
```

### Parametrized Tests

```python
import pytest

@pytest.mark.parametrize("event_type,unique_key", [
    ("fireflies.transcript.upload", "meeting-123"),
    ("fireflies.transcript.ready", "meeting-456"),
    ("test.event", "key-789"),
])
def test_event_id_generation(correlation_tracker, event_type, unique_key):
    """Test event ID generation with different inputs."""
    event_id_1 = correlation_tracker.generate_event_id(event_type, unique_key)
    event_id_2 = correlation_tracker.generate_event_id(event_type, unique_key)

    assert event_id_1 == event_id_2
```

## Test Coverage

### Coverage Goals

| Component | Target Coverage |
|-----------|----------------|
| Overall | ≥ 90% |
| correlation_tracker.py | 100% |
| rabbit.py (Publisher) | 95% |
| Error paths | 100% |

### Viewing Coverage

```bash
# Generate HTML report
pytest tests/ --cov=. --cov-report=html

# Open in browser
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux

# Terminal report
pytest tests/ --cov=. --cov-report=term-missing
```

### Coverage Configuration

Coverage settings in `pytest.ini`:
```ini
[coverage:run]
source = .
omit =
    tests/*
    .venv/*
    */site-packages/*

[coverage:report]
exclude_lines =
    pragma: no cover
    def __repr__
    raise AssertionError
    raise NotImplementedError
    if __name__ == .__main__.:
```

## CI/CD Integration

### GitHub Actions

Tests run automatically on:
- Push to `main` or `develop` branches
- Pull requests to `main` or `develop`
- Manual workflow dispatch

Workflow file: `.github/workflows/test.yml`

### Local CI Simulation

```bash
# Run full CI pipeline locally
make ci

# Or manually:
make clean
make install
make lint
make test
```

### Pre-commit Hooks

```bash
# Install pre-commit hooks
pip install pre-commit
pre-commit install

# Run manually
pre-commit run --all-files

# Skip hooks (not recommended)
git commit --no-verify
```

## Troubleshooting

### Common Issues

#### Import Errors

**Problem**: `ModuleNotFoundError` when running tests

**Solution**: Ensure parent directory is in Python path
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
```

#### Async Warnings

**Problem**: Warnings about unclosed event loops

**Solution**: Add `@pytest.mark.asyncio` decorator
```python
@pytest.mark.asyncio
async def test_something():
    pass
```

#### Fixture Not Found

**Problem**: `fixture 'correlation_tracker' not found`

**Solution**: Ensure fixtures are defined in `conftest.py` or imported:
```python
# In conftest.py
@pytest.fixture
async def correlation_tracker(fake_redis):
    # Fixture implementation
    pass
```

#### Redis Connection Errors

**Problem**: Tests trying to connect to real Redis

**Solution**: Ensure `fakeredis` is properly patched:
```python
with patch("correlation_tracker.redis.from_url", return_value=fake_redis):
    await tracker.start()
```

#### Slow Tests

**Problem**: Tests taking too long to run

**Solution**: Run in parallel with `pytest-xdist`:
```bash
pip install pytest-xdist
pytest tests/ -n auto
```

### Debug Techniques

#### Enable Verbose Logging

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

#### Use pdb Debugger

```python
@pytest.mark.asyncio
async def test_something():
    import pdb; pdb.set_trace()
    # Test code
```

#### Print Test Output

```bash
# Enable print statements
pytest tests/ -s

# Show captured output even for passing tests
pytest tests/ -s -v --capture=no
```

#### Isolate Test

```bash
# Run single test in isolation
pytest tests/test_correlation_tracking.py::test_specific_test -v
```

## Performance Optimization

### Expected Performance

- **Full test suite**: < 5 seconds
- **Individual test**: < 100ms
- **Parallel execution**: < 2 seconds (with `-n auto`)

### Optimization Tips

1. **Use fixtures wisely**: Session-scoped for expensive setup
2. **Mock external calls**: Avoid real network I/O
3. **Run in parallel**: Use `pytest-xdist` for multi-core execution
4. **Skip slow tests**: Mark with `@pytest.mark.slow` and skip when needed

```bash
# Run excluding slow tests
pytest tests/ -m "not slow"
```

## Best Practices

1. **Test Naming**
   - Use descriptive names: `test_feature_scenario`
   - Group related tests in classes: `TestFeatureName`

2. **Test Structure**
   - Follow AAA pattern: Arrange, Act, Assert
   - One concept per test (multiple asserts OK)

3. **Fixtures**
   - Use fixtures for common setup
   - Keep fixtures simple and focused
   - Document fixture purpose

4. **Assertions**
   - Use specific assertions: `assert x == y` not `assert x`
   - Add assertion messages for clarity
   - Test both positive and negative cases

5. **Coverage**
   - Aim for ≥90% overall coverage
   - 100% on critical paths
   - Don't obsess over 100% on everything

6. **Documentation**
   - Add docstrings to test classes and complex tests
   - Explain non-obvious test logic
   - Update README when adding test suites

## Resources

- [pytest documentation](https://docs.pytest.org/)
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [fakeredis](https://fakeredis.readthedocs.io/)
- [unittest.mock](https://docs.python.org/3/library/unittest.mock.html)
- [Coverage.py](https://coverage.readthedocs.io/)
