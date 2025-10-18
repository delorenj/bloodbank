# Bloodbank Event Publisher v2.0 - Test Suite

Comprehensive integration test suite for the bloodbank event publisher v2.0 with correlation tracking.

## Test Coverage

### 1. CorrelationTracker Initialization and Connection
- Default and custom parameter initialization
- Successful Redis connection establishment
- Graceful degradation when Redis is unavailable
- Idempotent start() calls
- Proper connection cleanup on close()

### 2. Deterministic Event ID Generation
- Same inputs produce identical UUIDs (idempotency)
- Different event types produce different UUIDs
- Different unique keys produce different UUIDs
- Namespace separation for multi-tenant scenarios
- Field order independence in idempotent ID generation

### 3. Adding Correlations
- Single parent correlation tracking
- Multiple parent correlation tracking (event merging scenarios)
- Custom metadata storage with correlations
- TTL enforcement on correlation data
- Graceful handling when tracker not started
- Convenience functions for common patterns

### 4. Querying Correlation Chains
- Retrieving immediate parents (single and multiple)
- Retrieving immediate children (single and multiple)
- Linear ancestor chain traversal (A → B → C → D)
- Linear descendant chain traversal
- Branching ancestor chains (multiple parents)
- Branching descendant chains (multiple children)
- Max depth protection against infinite loops
- Empty results for events without correlations

### 5. Graceful Degradation
- All operations work as no-ops when tracker not started
- Continued operation after Redis connection failure
- Empty/null responses instead of exceptions
- Non-blocking behavior for optional correlation tracking

### 6. Publisher Integration
- Publisher initialization with/without correlation tracking
- Publishing without correlation tracking
- Publishing with parent event correlations
- Multiple parent event correlations
- Deterministic event ID generation via Publisher
- Event ID extraction from message body
- Correlation metadata attachment
- RuntimeError when accessing tracking features when disabled

### 7. Debug Endpoints
- Basic debug_dump with parent/child relationships
- Debug dump with metadata inclusion
- Complex chain structure debugging
- Publisher debug_correlation method
- Direct metadata retrieval
- Null handling for non-existent correlations

### 8. Edge Cases and Error Handling
- Empty parent list handling
- Circular correlation detection with max_depth
- Concurrent correlation additions
- Large correlation chains (50+ events)
- Resource cleanup on Publisher.close()
- Timeout handling without blocking message publishing

## Prerequisites

```bash
# Install test dependencies
pip install -r tests/requirements-test.txt
```

## Running Tests

### Run all tests
```bash
pytest tests/
```

### Run with coverage report
```bash
pytest tests/ --cov=. --cov-report=html
open htmlcov/index.html  # View coverage report
```

### Run specific test suite
```bash
pytest tests/test_correlation_tracking.py::TestCorrelationTrackerInitialization
pytest tests/test_correlation_tracking.py::TestDeterministicEventIDGeneration
pytest tests/test_correlation_tracking.py::TestAddingCorrelations
pytest tests/test_correlation_tracking.py::TestQueryingCorrelationChains
pytest tests/test_correlation_tracking.py::TestGracefulDegradation
pytest tests/test_correlation_tracking.py::TestPublisherIntegration
pytest tests/test_correlation_tracking.py::TestDebugEndpoints
pytest tests/test_correlation_tracking.py::TestEdgeCasesAndErrorHandling
```

### Run specific test
```bash
pytest tests/test_correlation_tracking.py::TestDeterministicEventIDGeneration::test_same_inputs_produce_same_uuid -v
```

### Run with verbose output
```bash
pytest tests/ -vv
```

### Run with markers
```bash
pytest tests/ -m asyncio  # Run only async tests
pytest tests/ -m integration  # Run only integration tests
```

## Test Architecture

### Isolation Strategy
- **No external dependencies**: Uses `fakeredis` for Redis mocking
- **No RabbitMQ required**: Uses `AsyncMock` for RabbitMQ operations
- **Fast execution**: All tests run in memory without network I/O
- **Parallel-safe**: Tests are fully isolated and can run concurrently

### Factory Pattern
The test suite uses the Factory pattern for creating test data:

```python
event_factory = EventFactory()

# Create test source
source = event_factory.create_source(
    component="my-component",
    host_id="host-123"
)

# Create test Fireflies payload
payload = event_factory.create_fireflies_payload(
    meeting_id="meeting-abc",
    title="Team Standup"
)

# Create complete event envelope
envelope = event_factory.create_event_envelope(
    event_type="fireflies.transcript.ready",
    payload=payload,
    correlation_ids=[parent_event_id]
)
```

### Fixtures
The test suite provides comprehensive fixtures:

- `event_factory`: Factory for creating test events
- `fake_redis`: In-memory Redis instance using fakeredis
- `correlation_tracker`: Pre-configured CorrelationTracker with fake Redis
- `mock_rabbitmq`: Mocked RabbitMQ connection, channel, and exchange
- `publisher_with_tracking`: Publisher with correlation tracking enabled
- `publisher_without_tracking`: Publisher without correlation tracking

### Test Naming Convention
Tests follow a clear naming convention:
- `test_<feature>_<scenario>`: Descriptive test names
- Example: `test_get_parents_multiple_parents`
- Example: `test_publish_with_correlation`

## CI/CD Integration

### GitHub Actions
Tests run automatically on:
- Pull requests
- Pushes to main branch
- Manual workflow dispatch

See `.github/workflows/test.yml` for configuration.

### Pre-commit Hooks
```bash
# Install pre-commit hooks
pre-commit install

# Run manually
pre-commit run --all-files
```

## Code Coverage Goals

- **Overall coverage**: ≥ 90%
- **Critical paths**: 100% (correlation tracking, event publishing)
- **Error handling**: 100% (graceful degradation paths)

View detailed coverage reports after running tests:
```bash
open htmlcov/index.html
```

## Debugging Tests

### Run with detailed output
```bash
pytest tests/ -vv --tb=long
```

### Run with pdb on failure
```bash
pytest tests/ --pdb
```

### Run with print statements
```bash
pytest tests/ -s
```

### Run specific test with detailed logging
```bash
pytest tests/test_correlation_tracking.py::test_name -vv -s --log-cli-level=DEBUG
```

## Writing New Tests

### Test Structure
```python
@pytest.mark.asyncio
async def test_feature_description(correlation_tracker, event_factory):
    """Test that feature works as expected."""
    # Arrange: Set up test data
    parent_id = uuid4()
    child_id = uuid4()

    # Act: Perform the operation
    await correlation_tracker.add_correlation(child_id, [parent_id])

    # Assert: Verify the result
    parents = await correlation_tracker.get_parents(child_id)
    assert len(parents) == 1
    assert parents[0] == parent_id
```

### Best Practices
1. **Use descriptive test names** that explain what's being tested
2. **Follow AAA pattern**: Arrange, Act, Assert
3. **One assertion concept per test** (but multiple asserts are OK)
4. **Use fixtures** to avoid code duplication
5. **Test edge cases** and error conditions
6. **Mock external dependencies** (Redis, RabbitMQ)
7. **Keep tests fast** (< 1 second each)
8. **Make tests deterministic** (no random behavior)

## Troubleshooting

### fakeredis version issues
If you encounter issues with fakeredis:
```bash
pip install --upgrade fakeredis[aioredis]
```

### Async test warnings
If you see warnings about unclosed event loops:
```bash
# Add to your test
@pytest.mark.asyncio
async def test_something():
    # Your test code
    pass
```

### Import errors
Make sure the parent directory is in the Python path:
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
```

## Performance

### Test Execution Time
Expected execution times:
- Full test suite: < 5 seconds
- Individual test: < 100ms
- Slow tests marked with `@pytest.mark.slow`

### Optimization Tips
- Use `pytest-xdist` for parallel execution:
  ```bash
  pip install pytest-xdist
  pytest tests/ -n auto
  ```

## Contributing

When adding new features to bloodbank:

1. **Write tests first** (TDD approach)
2. **Ensure all tests pass** before submitting PR
3. **Maintain coverage** at ≥ 90%
4. **Add docstrings** to test classes and methods
5. **Update this README** if adding new test suites

## Resources

- [pytest documentation](https://docs.pytest.org/)
- [pytest-asyncio documentation](https://pytest-asyncio.readthedocs.io/)
- [fakeredis documentation](https://fakeredis.readthedocs.io/)
- [unittest.mock documentation](https://docs.python.org/3/library/unittest.mock.html)
