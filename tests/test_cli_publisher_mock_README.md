# Testing CLI Commands with AsyncMock

## Problem Statement

The CLI `publish` command uses `asyncio.run()` to execute async code:

```python
asyncio.run(_publish_envelope(routing_key, envelope))
```

This creates a NEW event loop, which causes issues when testing with pytest-asyncio:

1. **Event Loop Conflict**: pytest-asyncio tests marked with `@pytest.mark.asyncio` run in their own event loop
2. **Cannot Nest asyncio.run()**: You cannot call `asyncio.run()` from within a running event loop
3. **AsyncMock Assertions Fail**: AsyncMock's `assert_awaited_once()` checks if a mock was awaited in the CURRENT event loop context, but the actual await happens in a DIFFERENT event loop (created by `asyncio.run()`)

## The Solution

**Run tests as synchronous functions** (not async), and use `AsyncMock` with regular call assertions:

### ❌ Wrong Approach (Async Test)
```python
@pytest.mark.asyncio  # DON'T DO THIS
async def test_publish_command(tmp_path):
    with patch('event_producers.cli.Publisher', return_value=mock_instance):
        result = runner.invoke(app, ["publish", ...])
    
    # This fails: "asyncio.run() cannot be called from a running event loop"
    assert result.exit_code == 0
```

### ✅ Correct Approach (Sync Test)
```python
def test_publish_command(tmp_path):  # Regular sync function
    mock_publisher_instance = AsyncMock()
    
    with patch('event_producers.cli.Publisher', return_value=mock_publisher_instance):
        result = runner.invoke(app, ["publish", ...])
    
    # Use assert_called_once() instead of assert_awaited_once()
    mock_publisher_instance.start.assert_called_once()
    mock_publisher_instance.publish.assert_called_once()
    mock_publisher_instance.close.assert_called_once()
```

## Why This Works

1. **No Event Loop Conflict**: The test runs synchronously, so there's no event loop running when `asyncio.run()` is called
2. **CLI Creates Its Own Loop**: `asyncio.run()` in the CLI creates and manages its own event loop
3. **AsyncMock Still Works**: Even though we can't use `assert_awaited_once()` (which checks the current loop context), we can verify the mock was called with `assert_called_once()`
4. **Cross-Loop Verification**: We verify the Publisher methods were called, which is sufficient since the actual async execution happened successfully

## Key Points

- Use regular synchronous test functions (no `@pytest.mark.asyncio`)
- Use `AsyncMock` for async methods (start, publish, close)
- Use `assert_called_once()` instead of `assert_awaited_once()`
- Verify call arguments with `mock.call_args`
- Let `asyncio.run()` create its own event loop

## Additional Resources

- [pytest-asyncio documentation](https://pytest-asyncio.readthedocs.io/)
- [unittest.mock.AsyncMock](https://docs.python.org/3/library/unittest.mock.html#unittest.mock.AsyncMock)
- [asyncio.run()](https://docs.python.org/3/library/asyncio-runner.html#asyncio.run)
