# Bloodbank Tests - Quick Reference Card

## 🚀 Quick Start

```bash
# Install and run
pip install -r tests/requirements-test.txt
pytest tests/
```

## 📋 Common Commands

### Run Tests
```bash
pytest tests/                          # All tests
pytest tests/ -v                       # Verbose
pytest tests/ -vv                      # Extra verbose
make test                              # With coverage
make test-quick                        # Fast (no coverage)
```

### Coverage
```bash
make test-cov                          # Generate and open HTML report
pytest tests/ --cov=. --cov-report=term-missing
```

### Debug
```bash
pytest tests/ -s                       # Show print statements
pytest tests/ --pdb                    # Drop into debugger on failure
pytest tests/ --log-cli-level=DEBUG    # Debug logging
pytest tests/ -vv --tb=long            # Detailed tracebacks
```

### Filter Tests
```bash
pytest tests/ -k "test_correlation"    # Name pattern
pytest tests/ -m asyncio               # Async tests only
pytest tests/ -m integration           # Integration tests only
pytest tests/ -x                       # Stop on first failure
pytest tests/ --lf                     # Re-run last failed
```

## 🏗️ Test Structure

### Test Suites (80+ tests)
1. **TestCorrelationTrackerInitialization** (6 tests)
2. **TestDeterministicEventIDGeneration** (6 tests)
3. **TestAddingCorrelations** (7 tests)
4. **TestQueryingCorrelationChains** (11 tests)
5. **TestGracefulDegradation** (6 tests)
6. **TestPublisherIntegration** (13 tests)
7. **TestDebugEndpoints** (7 tests)
8. **TestEdgeCasesAndErrorHandling** (10+ tests)

### Run Specific Suite
```bash
pytest tests/test_correlation_tracking.py::TestCorrelationTrackerInitialization -v
```

## 🔧 Available Fixtures

```python
fake_redis                    # In-memory Redis (fakeredis)
correlation_tracker           # Pre-configured tracker
mock_rabbitmq                 # Mocked RabbitMQ
publisher_with_tracking       # Publisher with correlation enabled
publisher_without_tracking    # Publisher without correlation
event_factory                 # Factory for test events
test_config                   # Test configuration
```

## 📝 Writing Tests

### Basic Template
```python
@pytest.mark.asyncio
async def test_feature(correlation_tracker):
    """Test description."""
    # Arrange
    parent_id = uuid4()
    child_id = uuid4()

    # Act
    await correlation_tracker.add_correlation(child_id, [parent_id])

    # Assert
    parents = await correlation_tracker.get_parents(child_id)
    assert parents == [parent_id]
```

### Using Event Factory
```python
def test_with_factory(event_factory):
    payload = event_factory.create_fireflies_payload(
        meeting_id="meeting-123"
    )
    envelope = event_factory.create_event_envelope(
        event_type="test.event",
        payload=payload
    )
    assert envelope.payload.id == "meeting-123"
```

## 🎯 Makefile Shortcuts

```bash
make help                # Show all targets
make install             # Install dependencies
make test                # Run tests with coverage
make test-cov            # Run tests and open coverage report
make test-quick          # Fast test run
make test-parallel       # Parallel execution
make test-correlation    # Test correlation module
make test-publisher      # Test publisher module
make lint                # Run linting
make format              # Format code
make clean               # Clean generated files
make check               # Lint + test
make ci                  # Full CI pipeline
```

## 📊 Coverage Goals

| Component | Target |
|-----------|--------|
| Overall | ≥ 90% |
| correlation_tracker.py | 100% |
| rabbit.py | 95% |
| Error paths | 100% |

## 🐛 Debugging Tricks

### Drop into debugger
```python
import pdb; pdb.set_trace()
```

### Enable verbose logging
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Show fixture values
```bash
pytest tests/ --fixtures           # List all fixtures
pytest tests/ --setup-show         # Show fixture setup/teardown
```

### Run specific test with debugging
```bash
pytest tests/test_correlation_tracking.py::test_name -vv -s --pdb
```

## 🔍 Finding Tests

### Test Files
- `tests/test_correlation_tracking.py` - Main test suite (1,305 lines)
- `tests/conftest.py` - Shared fixtures
- `tests/requirements-test.txt` - Dependencies

### Documentation
- `tests/README.md` - Overview and usage
- `tests/TESTING_GUIDE.md` - Comprehensive guide
- `tests/TEST_SUMMARY.md` - High-level summary
- `tests/QUICK_REFERENCE.md` - This file

## ⚡ Performance

- **Full suite**: < 5 seconds
- **Individual test**: < 100ms
- **Parallel execution**: `pytest tests/ -n auto` (requires pytest-xdist)

## 🔄 CI/CD

### GitHub Actions
- Triggers: Push to main/develop, pull requests
- Python versions: 3.11, 3.12
- Coverage: Uploaded to Codecov
- Workflow: `.github/workflows/test.yml`

### Pre-commit Hooks
```bash
pre-commit install              # Install hooks
pre-commit run --all-files      # Run manually
```

## 📦 Dependencies

```txt
pytest>=8.0.0
pytest-asyncio>=0.23.0
pytest-cov>=4.1.0
fakeredis[aioredis]>=2.21.0
pytest-mock>=3.12.0
black>=24.0.0
ruff>=0.1.0
mypy>=1.8.0
```

## 🎓 Test Patterns

### Pattern 1: Idempotency
```python
id1 = tracker.generate_event_id("type", "key")
id2 = tracker.generate_event_id("type", "key")
assert id1 == id2
```

### Pattern 2: Correlation Chain
```python
await tracker.add_correlation(b, [a])
await tracker.add_correlation(c, [b])
chain = await tracker.get_correlation_chain(c, "ancestors")
```

### Pattern 3: Graceful Degradation
```python
tracker = CorrelationTracker()  # Don't start
await tracker.add_correlation(child, [parent])
assert await tracker.get_parents(child) == []
```

## ❓ Common Issues

### Import Error?
```bash
# Ensure path is correct
python -m pytest tests/
```

### Fixture Not Found?
- Check `conftest.py` for fixture definition
- Ensure fixture scope is correct

### Test Hangs?
- Add timeout: `pytest tests/ --timeout=10`
- Check for missing `@pytest.mark.asyncio`

### Redis Connection Error?
- Ensure using `fake_redis` fixture
- Check `fakeredis` is installed

## 📚 Resources

- [pytest docs](https://docs.pytest.org/)
- [pytest-asyncio docs](https://pytest-asyncio.readthedocs.io/)
- [fakeredis docs](https://fakeredis.readthedocs.io/)

## 🎯 Quick Verification

```bash
# Verify everything works
pip install -r tests/requirements-test.txt
make clean
make test
make test-cov
```

---

**Quick Reference Version**: 1.0.0
**Last Updated**: 2025-10-18
**For Full Details**: See `tests/README.md` and `tests/TESTING_GUIDE.md`
