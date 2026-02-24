"""
Unit tests for FSM Manager - command acceptance, guards, transitions.
"""

import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import fakeredis

from command_fsm import FSMManager, FSMState, CommandGuardResult, AgentFSMSnapshot


@pytest.fixture
def redis_client():
    """Fake Redis client for testing with Lua script support"""
    server = fakeredis.FakeServer()
    return fakeredis.FakeRedis(server=server, decode_responses=False)


@pytest.fixture
def fsm_manager(redis_client):
    """FSM manager with fake Redis"""
    manager = FSMManager()
    manager.redis = redis_client
    # Reinitialize stores with fake Redis
    from command_fsm.redis_store import RedisAgentFSMStore
    from command_fsm.idempotency import IdempotencyStore
    manager.fsm_store = RedisAgentFSMStore(redis_client)
    manager.idemp_store = IdempotencyStore(redis_client)
    return manager


def test_initialize_agent(fsm_manager):
    """Test agent initialization to idle state"""
    state = fsm_manager.get_or_initialize("lenoon")
    
    assert state.agent_name == "lenoon"
    assert state.state == FSMState.IDLE
    assert state.version == 1
    assert state.command_id is None
    assert state.pre_pause_state is None
    assert state.ttl_deadline is None


def test_accept_command_success(fsm_manager):
    """Test successful command acceptance (all guards pass)"""
    command_id = uuid4()
    issued_at = datetime.now(timezone.utc)
    
    result, state = fsm_manager.accept_command(
        agent_name="lenoon",
        command_id=command_id,
        issued_at=issued_at,
        ttl_ms=30000
    )
    
    assert result == CommandGuardResult.PASSED
    assert state.state == FSMState.ACKNOWLEDGING
    assert state.command_id == command_id
    assert state.version == 2  # Version incremented (1 → 2)
    assert state.ttl_deadline is not None


def test_accept_command_expired(fsm_manager):
    """Test command rejection when TTL expired"""
    command_id = uuid4()
    issued_at = datetime.now(timezone.utc) - timedelta(seconds=60)  # Issued 60s ago
    
    result, state = fsm_manager.accept_command(
        agent_name="lenoon",
        command_id=command_id,
        issued_at=issued_at,
        ttl_ms=30000  # TTL only 30s
    )
    
    assert result == CommandGuardResult.EXPIRED
    assert state.state == FSMState.IDLE  # State unchanged


def test_accept_command_duplicate(fsm_manager):
    """Test idempotency: second command with same key is skipped"""
    command_id_1 = uuid4()
    command_id_2 = uuid4()
    issued_at = datetime.now(timezone.utc)
    idemp_key = "test-drift-check-123"
    
    # First command: accepted
    result1, state1 = fsm_manager.accept_command(
        agent_name="lenoon",
        command_id=command_id_1,
        issued_at=issued_at,
        ttl_ms=30000,
        idempotency_key=idemp_key
    )
    assert result1 == CommandGuardResult.PASSED
    
    # Reset to idle for second attempt
    fsm_manager.mark_working("lenoon")
    fsm_manager.mark_completed("lenoon")
    
    # Second command with same key: rejected as duplicate
    result2, state2 = fsm_manager.accept_command(
        agent_name="lenoon",
        command_id=command_id_2,
        issued_at=issued_at,
        ttl_ms=30000,
        idempotency_key=idemp_key
    )
    assert result2 == CommandGuardResult.DUPLICATE
    assert state2.state == FSMState.IDLE  # State unchanged


def test_accept_command_not_idle(fsm_manager):
    """Test command rejection when agent not in idle state"""
    command_id_1 = uuid4()
    command_id_2 = uuid4()
    issued_at = datetime.now(timezone.utc)
    
    # First command: accepted, transitions to acknowledging
    result1, state1 = fsm_manager.accept_command(
        agent_name="lenoon",
        command_id=command_id_1,
        issued_at=issued_at,
        ttl_ms=30000
    )
    assert result1 == CommandGuardResult.PASSED
    assert state1.state == FSMState.ACKNOWLEDGING
    
    # Second command while still acknowledging: rejected
    result2, state2 = fsm_manager.accept_command(
        agent_name="lenoon",
        command_id=command_id_2,
        issued_at=issued_at,
        ttl_ms=30000
    )
    assert result2 == CommandGuardResult.INVALID_STATE
    assert state2.state == FSMState.ACKNOWLEDGING  # State unchanged


def test_accept_command_paused(fsm_manager):
    """Test command rejection when agent is paused"""
    # Initialize and pause
    fsm_manager.get_or_initialize("lenoon")
    fsm_manager.pause("lenoon")
    
    command_id = uuid4()
    issued_at = datetime.now(timezone.utc)
    
    result, state = fsm_manager.accept_command(
        agent_name="lenoon",
        command_id=command_id,
        issued_at=issued_at,
        ttl_ms=30000
    )
    
    assert result == CommandGuardResult.PAUSED
    assert state.state == FSMState.PAUSED


def test_full_command_lifecycle(fsm_manager):
    """Test complete command flow: accept → ack → working → completed"""
    command_id = uuid4()
    issued_at = datetime.now(timezone.utc)
    
    # 1. Accept command (idle → acknowledging)
    result, state = fsm_manager.accept_command(
        agent_name="lenoon",
        command_id=command_id,
        issued_at=issued_at,
        ttl_ms=30000
    )
    assert result == CommandGuardResult.PASSED
    assert state.state == FSMState.ACKNOWLEDGING
    assert state.version == 2
    
    # 2. Mark working (acknowledging → working)
    success, state = fsm_manager.mark_working("lenoon")
    assert success is True
    assert state.state == FSMState.WORKING
    assert state.version == 3
    
    # 3. Mark completed (working → idle)
    success, state = fsm_manager.mark_completed("lenoon")
    assert success is True
    assert state.state == FSMState.IDLE
    assert state.version == 4
    assert state.command_id is None  # Command cleared


def test_command_failure(fsm_manager):
    """Test command failure flow: accept → ack → working → error"""
    command_id = uuid4()
    issued_at = datetime.now(timezone.utc)
    
    # Accept and mark working
    fsm_manager.accept_command("lenoon", command_id, issued_at, 30000)
    fsm_manager.mark_working("lenoon")
    
    # Mark failed (working → error)
    success, state = fsm_manager.mark_failed("lenoon")
    assert success is True
    assert state.state == FSMState.ERROR
    assert state.command_id == command_id  # Command ID retained for error tracking


def test_pause_and_resume(fsm_manager):
    """Test pause/resume preserves pre-pause state"""
    command_id = uuid4()
    issued_at = datetime.now(timezone.utc)
    
    # Accept and mark working
    fsm_manager.accept_command("lenoon", command_id, issued_at, 30000)
    fsm_manager.mark_working("lenoon")
    
    # Pause while working
    success, state = fsm_manager.pause("lenoon")
    assert success is True
    assert state.state == FSMState.PAUSED
    assert state.pre_pause_state == FSMState.WORKING
    
    # Resume (should return to working)
    success, state = fsm_manager.resume("lenoon")
    assert success is True
    assert state.state == FSMState.WORKING
    assert state.pre_pause_state is None


def test_reset_to_idle(fsm_manager):
    """Test force reset bypasses CAS and returns to idle"""
    command_id = uuid4()
    issued_at = datetime.now(timezone.utc)
    
    # Accept and mark working
    fsm_manager.accept_command("lenoon", command_id, issued_at, 30000)
    fsm_manager.mark_working("lenoon")
    
    # Force reset
    state = fsm_manager.reset("lenoon")
    assert state.state == FSMState.IDLE
    assert state.command_id is None
    assert state.version > 3  # Version incremented


def test_cas_retry_on_conflict(fsm_manager, redis_client):
    """Test CAS retry succeeds after version conflict"""
    # Initialize agent
    state = fsm_manager.get_or_initialize("lenoon")
    
    # Simulate external version bump (mimics concurrent transition)
    key = "agent:lenoon:fsm"
    redis_client.hincrby(key, 'version', 1)
    
    # Transition should retry and succeed
    success, new_state = fsm_manager.transition_with_retry(
        agent_name="lenoon",
        expected_version=state.version,
        target_state=FSMState.PAUSED,
        pre_pause_state=FSMState.IDLE
    )
    
    assert success is True  # Should succeed after retry
    assert new_state.state == FSMState.PAUSED


def test_cas_retry_exhausted(fsm_manager, redis_client, monkeypatch):
    """Test CAS retry failure when max retries exceeded"""
    # Reduce retry limit for test
    monkeypatch.setattr(fsm_manager, 'MAX_CAS_RETRIES', 1)
    
    state = fsm_manager.get_or_initialize("lenoon")
    
    # Continuously bump version to force retry failure
    def mock_transition(*args, **kwargs):
        key = "agent:lenoon:fsm"
        current_version = int(redis_client.hget(key, 'version'))
        redis_client.hincrby(key, 'version', 1)
        return (False, current_version + 1)
    
    monkeypatch.setattr(fsm_manager.fsm_store, 'transition', mock_transition)
    
    success, final_state = fsm_manager.transition_with_retry(
        agent_name="lenoon",
        expected_version=state.version,
        target_state=FSMState.PAUSED
    )
    
    assert success is False  # Should fail after retries exhausted


def test_idempotency_ttl(fsm_manager):
    """Test idempotency key has correct TTL"""
    command_id = uuid4()
    issued_at = datetime.now(timezone.utc)
    idemp_key = "test-ttl-check"
    
    # Accept command with idempotency key
    fsm_manager.accept_command(
        agent_name="lenoon",
        command_id=command_id,
        issued_at=issued_at,
        ttl_ms=30000,
        idempotency_key=idemp_key
    )
    
    # Check TTL is set and reasonable (should be ~300s)
    ttl = fsm_manager.idemp_store.get_ttl("lenoon", idemp_key)
    assert 290 <= ttl <= 300  # Allow some slack for test execution time


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
