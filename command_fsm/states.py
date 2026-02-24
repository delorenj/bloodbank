"""
FSM state definitions and transition rules for agent command processing.

States:
- idle: No active command, ready for work
- acknowledging: Command received, ack being prepared
- working: Actively executing a command
- blocked: Execution stalled on external dependency
- error: Command failed, awaiting retry or intervention
- paused: Manually paused (by human or agent self)

Transitions are guarded by conditions (TTL check, idempotency, version conflicts).
"""

from enum import Enum
from typing import Optional, Set, Dict
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID


class FSMState(str, Enum):
    """Agent FSM states"""
    IDLE = "idle"
    ACKNOWLEDGING = "acknowledging"
    WORKING = "working"
    BLOCKED = "blocked"
    ERROR = "error"
    PAUSED = "paused"


@dataclass
class StateTransition:
    """Represents a valid state transition with its guard conditions"""
    from_state: FSMState
    to_state: FSMState
    guard_description: str
    side_effect: Optional[str] = None  # Event to emit on successful transition


# Transition matrix: (from_state, to_state) -> guard description
TRANSITIONS: Dict[tuple[FSMState, FSMState], StateTransition] = {
    # idle → acknowledging: new command arrives and passes guards
    (FSMState.IDLE, FSMState.ACKNOWLEDGING): StateTransition(
        from_state=FSMState.IDLE,
        to_state=FSMState.ACKNOWLEDGING,
        guard_description="Command not expired (TTL check). Agent not paused. Idempotency key not seen.",
        side_effect="Publish command.{agent}.{action}.ack"
    ),
    
    # acknowledging → working: ack published successfully
    (FSMState.ACKNOWLEDGING, FSMState.WORKING): StateTransition(
        from_state=FSMState.ACKNOWLEDGING,
        to_state=FSMState.WORKING,
        guard_description="Ack published successfully.",
        side_effect="Publish agent.{name}.state.changed (state=working)"
    ),
    
    # working → idle: command completed successfully
    (FSMState.WORKING, FSMState.IDLE): StateTransition(
        from_state=FSMState.WORKING,
        to_state=FSMState.IDLE,
        guard_description="Result produced.",
        side_effect="Publish command.{agent}.{action}.result + agent.{name}.state.changed (state=idle)"
    ),
    
    # working → error: execution failed or timeout
    (FSMState.WORKING, FSMState.ERROR): StateTransition(
        from_state=FSMState.WORKING,
        to_state=FSMState.ERROR,
        guard_description="execute() failed OR watchdog timeout.",
        side_effect="Publish command.{agent}.{action}.error + agent.{name}.state.changed (state=error)"
    ),
    
    # working → blocked: agent self-reports blockage
    (FSMState.WORKING, FSMState.BLOCKED): StateTransition(
        from_state=FSMState.WORKING,
        to_state=FSMState.BLOCKED,
        guard_description="Agent self-reports blockage on external dependency.",
        side_effect="Publish agent.{name}.state.changed (state=blocked)"
    ),
    
    # blocked → working: dependency resolved
    (FSMState.BLOCKED, FSMState.WORKING): StateTransition(
        from_state=FSMState.BLOCKED,
        to_state=FSMState.WORKING,
        guard_description="Dependency resolved.",
        side_effect="Publish agent.{name}.state.changed (state=working)"
    ),
    
    # blocked → error: block timeout exceeded
    (FSMState.BLOCKED, FSMState.ERROR): StateTransition(
        from_state=FSMState.BLOCKED,
        to_state=FSMState.ERROR,
        guard_description="Block timeout exceeded.",
        side_effect="Publish command.{agent}.{action}.error"
    ),
    
    # error → idle: manual reset or auto-retry exhausted
    (FSMState.ERROR, FSMState.IDLE): StateTransition(
        from_state=FSMState.ERROR,
        to_state=FSMState.IDLE,
        guard_description="Manual reset or auto-retry exhausted.",
        side_effect="Publish agent.{name}.state.changed (state=idle)"
    ),
    
    # Any state → paused: explicit pause command
    (FSMState.IDLE, FSMState.PAUSED): StateTransition(
        from_state=FSMState.IDLE,
        to_state=FSMState.PAUSED,
        guard_description="Explicit pause command.",
        side_effect="Store pre-pause state. Publish state.changed (state=paused)"
    ),
    (FSMState.ACKNOWLEDGING, FSMState.PAUSED): StateTransition(
        from_state=FSMState.ACKNOWLEDGING,
        to_state=FSMState.PAUSED,
        guard_description="Explicit pause command.",
        side_effect="Store pre-pause state. Publish state.changed (state=paused)"
    ),
    (FSMState.WORKING, FSMState.PAUSED): StateTransition(
        from_state=FSMState.WORKING,
        to_state=FSMState.PAUSED,
        guard_description="Explicit pause command.",
        side_effect="Store pre-pause state. Publish state.changed (state=paused)"
    ),
    (FSMState.BLOCKED, FSMState.PAUSED): StateTransition(
        from_state=FSMState.BLOCKED,
        to_state=FSMState.PAUSED,
        guard_description="Explicit pause command.",
        side_effect="Store pre-pause state. Publish state.changed (state=paused)"
    ),
    (FSMState.ERROR, FSMState.PAUSED): StateTransition(
        from_state=FSMState.ERROR,
        to_state=FSMState.PAUSED,
        guard_description="Explicit pause command.",
        side_effect="Store pre-pause state. Publish state.changed (state=paused)"
    ),
    
    # paused → {previous}: explicit resume command
    # Note: The "to_state" for resume is dynamic (stored in pre_pause_state)
    # These are placeholders; actual resume logic checks pre_pause_state
    (FSMState.PAUSED, FSMState.IDLE): StateTransition(
        from_state=FSMState.PAUSED,
        to_state=FSMState.IDLE,
        guard_description="Explicit resume command. pre_pause_state=idle.",
        side_effect="Restore pre-pause state. Publish state.changed"
    ),
    (FSMState.PAUSED, FSMState.WORKING): StateTransition(
        from_state=FSMState.PAUSED,
        to_state=FSMState.WORKING,
        guard_description="Explicit resume command. pre_pause_state=working.",
        side_effect="Restore pre-pause state. Publish state.changed"
    ),
    (FSMState.PAUSED, FSMState.BLOCKED): StateTransition(
        from_state=FSMState.PAUSED,
        to_state=FSMState.BLOCKED,
        guard_description="Explicit resume command. pre_pause_state=blocked.",
        side_effect="Restore pre-pause state. Publish state.changed"
    ),
    (FSMState.PAUSED, FSMState.ERROR): StateTransition(
        from_state=FSMState.PAUSED,
        to_state=FSMState.ERROR,
        guard_description="Explicit resume command. pre_pause_state=error.",
        side_effect="Restore pre-pause state. Publish state.changed"
    ),
}


def is_valid_transition(from_state: FSMState, to_state: FSMState) -> bool:
    """Check if a state transition is valid per the transition matrix"""
    return (from_state, to_state) in TRANSITIONS


def get_allowed_transitions(from_state: FSMState) -> Set[FSMState]:
    """Get all valid target states from a given state"""
    return {to for (frm, to) in TRANSITIONS.keys() if frm == from_state}


def get_transition(from_state: FSMState, to_state: FSMState) -> Optional[StateTransition]:
    """Get the transition rule for a given state change"""
    return TRANSITIONS.get((from_state, to_state))


@dataclass
class AgentFSMSnapshot:
    """Current FSM state for an agent (read from Redis)"""
    agent_name: str
    state: FSMState
    version: int  # Monotonic counter for optimistic concurrency
    command_id: Optional[UUID]  # Currently processing command (None if idle)
    entered_at: datetime  # When we entered this state
    pre_pause_state: Optional[FSMState]  # State before pause (None if not paused)
    ttl_deadline: Optional[datetime]  # Command expiry time (None if no TTL)
    
    def is_expired(self) -> bool:
        """Check if the current command has expired"""
        if not self.ttl_deadline:
            return False
        return datetime.now(timezone.utc) > self.ttl_deadline
    
    def can_transition_to(self, target_state: FSMState) -> bool:
        """Check if transition to target state is valid"""
        return is_valid_transition(self.state, target_state)
