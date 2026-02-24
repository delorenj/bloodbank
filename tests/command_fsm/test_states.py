"""
Unit tests for FSM state definitions and transition rules.
"""

import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from command_fsm.states import (
    FSMState,
    StateTransition,
    is_valid_transition,
    get_allowed_transitions,
    get_transition,
    AgentFSMSnapshot,
    TRANSITIONS
)


def test_all_states_defined():
    """Verify all FSM states are present"""
    expected_states = {"idle", "acknowledging", "working", "blocked", "error", "paused"}
    actual_states = {state.value for state in FSMState}
    assert actual_states == expected_states


def test_valid_transitions_from_idle():
    """Test valid transitions from idle state"""
    allowed = get_allowed_transitions(FSMState.IDLE)
    assert FSMState.ACKNOWLEDGING in allowed
    assert FSMState.PAUSED in allowed
    assert len(allowed) == 2  # idle can only go to acknowledging or paused


def test_valid_transitions_from_working():
    """Test valid transitions from working state"""
    allowed = get_allowed_transitions(FSMState.WORKING)
    assert FSMState.IDLE in allowed  # Completed
    assert FSMState.ERROR in allowed  # Failed
    assert FSMState.BLOCKED in allowed  # Blocked on dependency
    assert FSMState.PAUSED in allowed  # Manual pause
    assert len(allowed) == 4


def test_invalid_transition():
    """Test invalid state transition detection"""
    # idle → working is invalid (must go through acknowledging)
    assert is_valid_transition(FSMState.IDLE, FSMState.WORKING) is False
    
    # acknowledging → idle is invalid (must go through working)
    assert is_valid_transition(FSMState.ACKNOWLEDGING, FSMState.IDLE) is False
    
    # error → working is invalid (must reset to idle first)
    assert is_valid_transition(FSMState.ERROR, FSMState.WORKING) is False


def test_pause_from_any_state():
    """Test that pause is allowed from all non-paused states"""
    non_paused_states = [FSMState.IDLE, FSMState.ACKNOWLEDGING, FSMState.WORKING, FSMState.BLOCKED, FSMState.ERROR]
    
    for state in non_paused_states:
        assert is_valid_transition(state, FSMState.PAUSED), f"Pause should be allowed from {state.value}"


def test_resume_to_valid_states():
    """Test resume can only return to specific states"""
    allowed_resume_targets = get_allowed_transitions(FSMState.PAUSED)
    
    # Resume can go back to idle, working, blocked, or error
    # (acknowledging is transient, shouldn't be paused from)
    assert FSMState.IDLE in allowed_resume_targets
    assert FSMState.WORKING in allowed_resume_targets
    assert FSMState.BLOCKED in allowed_resume_targets
    assert FSMState.ERROR in allowed_resume_targets


def test_transition_has_guard_description():
    """Test that all transitions have guard descriptions"""
    for (from_state, to_state), transition in TRANSITIONS.items():
        assert transition.guard_description, f"Missing guard for {from_state.value} → {to_state.value}"
        assert len(transition.guard_description) > 10, "Guard description too short"


def test_transition_side_effects():
    """Test that critical transitions have side effects defined"""
    # idle → acknowledging should emit ack
    t = get_transition(FSMState.IDLE, FSMState.ACKNOWLEDGING)
    assert t is not None
    assert "ack" in t.side_effect.lower()
    
    # working → idle should emit result
    t = get_transition(FSMState.WORKING, FSMState.IDLE)
    assert t is not None
    assert "result" in t.side_effect.lower()
    
    # working → error should emit error event
    t = get_transition(FSMState.WORKING, FSMState.ERROR)
    assert t is not None
    assert "error" in t.side_effect.lower()


def test_agent_fsm_snapshot_expired():
    """Test command expiry detection in snapshot"""
    now = datetime.now(timezone.utc)
    
    # Not expired: TTL deadline in future
    snapshot_valid = AgentFSMSnapshot(
        agent_name="lenoon",
        state=FSMState.WORKING,
        version=5,
        command_id=uuid4(),
        entered_at=now,
        pre_pause_state=None,
        ttl_deadline=now + timedelta(seconds=10)
    )
    assert snapshot_valid.is_expired() is False
    
    # Expired: TTL deadline in past
    snapshot_expired = AgentFSMSnapshot(
        agent_name="lenoon",
        state=FSMState.WORKING,
        version=5,
        command_id=uuid4(),
        entered_at=now - timedelta(seconds=60),
        pre_pause_state=None,
        ttl_deadline=now - timedelta(seconds=30)
    )
    assert snapshot_expired.is_expired() is True
    
    # No TTL: never expires
    snapshot_no_ttl = AgentFSMSnapshot(
        agent_name="lenoon",
        state=FSMState.WORKING,
        version=5,
        command_id=uuid4(),
        entered_at=now,
        pre_pause_state=None,
        ttl_deadline=None
    )
    assert snapshot_no_ttl.is_expired() is False


def test_agent_fsm_snapshot_can_transition():
    """Test transition validation on snapshot"""
    snapshot = AgentFSMSnapshot(
        agent_name="lenoon",
        state=FSMState.IDLE,
        version=1,
        command_id=None,
        entered_at=datetime.now(timezone.utc),
        pre_pause_state=None,
        ttl_deadline=None
    )
    
    # Valid: idle → acknowledging
    assert snapshot.can_transition_to(FSMState.ACKNOWLEDGING) is True
    
    # Invalid: idle → working (must go through acknowledging)
    assert snapshot.can_transition_to(FSMState.WORKING) is False


def test_transition_matrix_completeness():
    """Verify transition matrix covers expected paths"""
    # Count transitions per state
    from collections import defaultdict
    transitions_from = defaultdict(set)
    
    for (from_state, to_state) in TRANSITIONS.keys():
        transitions_from[from_state].add(to_state)
    
    # Idle should have at least 2 transitions
    assert len(transitions_from[FSMState.IDLE]) >= 2
    
    # Working should have at least 4 transitions (idle, error, blocked, paused)
    assert len(transitions_from[FSMState.WORKING]) >= 4
    
    # Paused should allow resume to multiple states
    assert len(transitions_from[FSMState.PAUSED]) >= 3


def test_no_self_transitions():
    """Verify no state transitions to itself (except via explicit logic)"""
    for (from_state, to_state) in TRANSITIONS.keys():
        assert from_state != to_state, f"Self-transition detected: {from_state.value} → {to_state.value}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
