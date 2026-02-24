"""
Agent Command FSM - Redis-backed finite state machine for command processing.

Main components:
- FSMManager: High-level API for command acceptance and state transitions
- FSMState: Enum of valid states (idle, acknowledging, working, blocked, error, paused)
- AgentFSMSnapshot: Current state snapshot with version and metadata
- CommandGuardResult: Result of guard checks (passed, expired, duplicate, etc.)

Usage:
    from command_fsm import FSMManager, FSMState, CommandGuardResult
    
    manager = FSMManager(redis_url="redis://localhost:6379")
    
    # Accept a command
    result, state = manager.accept_command(
        agent_name="lenoon",
        command_id=uuid4(),
        issued_at=datetime.now(timezone.utc),
        ttl_ms=30000,
        idempotency_key="drift-check-2026-02-24"
    )
    
    if result == CommandGuardResult.PASSED:
        # Publish ack, then mark working
        success, state = manager.mark_working("lenoon")
        
        # ... execute command ...
        
        # Mark completed
        success, state = manager.mark_completed("lenoon")
"""

from .manager import FSMManager, CommandGuardResult
from .states import FSMState, AgentFSMSnapshot
from .redis_store import RedisAgentFSMStore
from .idempotency import IdempotencyStore

__all__ = [
    "FSMManager",
    "CommandGuardResult",
    "FSMState",
    "AgentFSMSnapshot",
    "RedisAgentFSMStore",
    "IdempotencyStore",
]
