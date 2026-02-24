"""
High-level FSM manager that orchestrates state transitions with guards and idempotency.

This is the main API for command processing. The manager:
1. Checks guards (TTL, idempotency, FSM state)
2. Attempts CAS transition
3. Retries on version conflicts
4. Returns actionable results (ack/skip/reject/error)
"""

import redis
from typing import Optional, Tuple
from datetime import datetime, timezone, timedelta
from uuid import UUID
from enum import Enum
import logging

from .states import FSMState, AgentFSMSnapshot, is_valid_transition
from .redis_store import RedisAgentFSMStore
from .idempotency import IdempotencyStore

logger = logging.getLogger(__name__)


class CommandGuardResult(str, Enum):
    """Result of guard check before command execution"""
    PASSED = "passed"  # All guards passed, proceed with ack
    EXPIRED = "expired"  # Command TTL expired before processing
    DUPLICATE = "duplicate"  # Idempotency key seen before (skip execution)
    INVALID_STATE = "invalid_state"  # Agent not in valid state for this command
    PAUSED = "paused"  # Agent is paused
    VERSION_CONFLICT = "version_conflict"  # CAS retry limit exceeded


class FSMManager:
    """High-level FSM orchestration with guards and idempotency"""
    
    MAX_CAS_RETRIES = 3  # Max retries on version conflicts
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis = redis.from_url(redis_url, decode_responses=False)
        self.fsm_store = RedisAgentFSMStore(self.redis)
        self.idemp_store = IdempotencyStore(self.redis)
    
    def get_or_initialize(self, agent_name: str) -> AgentFSMSnapshot:
        """Get agent FSM state, initializing if it doesn't exist"""
        state = self.fsm_store.get_state(agent_name)
        if not state:
            state = self.fsm_store.initialize_state(agent_name)
        return state
    
    def check_command_guards(
        self,
        agent_name: str,
        command_id: UUID,
        issued_at: datetime,
        ttl_ms: int,
        idempotency_key: Optional[str] = None
    ) -> Tuple[CommandGuardResult, Optional[AgentFSMSnapshot]]:
        """
        Check all guards before accepting a command.
        
        Returns:
            (result, current_state)
            - result: Guard check outcome
            - current_state: Current FSM state (None if not initialized)
        """
        # 1. Get current state
        state = self.get_or_initialize(agent_name)
        
        # 2. Check if agent is paused
        if state.state == FSMState.PAUSED:
            logger.warning(f"Command {command_id} rejected: agent {agent_name} is paused")
            return (CommandGuardResult.PAUSED, state)
        
        # 3. Check if agent is in valid state (must be idle to accept new commands)
        if state.state != FSMState.IDLE:
            logger.warning(f"Command {command_id} rejected: agent {agent_name} not idle (current state: {state.state.value})")
            return (CommandGuardResult.INVALID_STATE, state)
        
        # 4. Check TTL (if > 0)
        if ttl_ms > 0:
            deadline = issued_at + timedelta(milliseconds=ttl_ms)
            if datetime.now(timezone.utc) > deadline:
                logger.warning(f"Command {command_id} expired (issued {issued_at}, TTL {ttl_ms}ms)")
                return (CommandGuardResult.EXPIRED, state)
        
        # 5. Check idempotency (if key provided)
        if idempotency_key:
            is_new = self.idemp_store.check_and_record(agent_name, idempotency_key, command_id)
            if not is_new:
                logger.info(f"Command {command_id} is duplicate (idempotency key: {idempotency_key})")
                return (CommandGuardResult.DUPLICATE, state)
        
        # All guards passed
        return (CommandGuardResult.PASSED, state)
    
    def transition_with_retry(
        self,
        agent_name: str,
        expected_version: int,
        target_state: FSMState,
        command_id: Optional[UUID] = None,
        pre_pause_state: Optional[FSMState] = None,
        ttl_deadline: Optional[datetime] = None
    ) -> Tuple[bool, AgentFSMSnapshot]:
        """
        Attempt state transition with automatic retry on version conflicts.
        
        Returns:
            (success, final_state)
            - success: True if transition succeeded (possibly after retries)
            - final_state: FSM state after transition (or current state if failed)
        """
        current_version = expected_version
        
        for attempt in range(self.MAX_CAS_RETRIES):
            success, new_version = self.fsm_store.transition(
                agent_name=agent_name,
                expected_version=current_version,
                new_state=target_state,
                command_id=command_id,
                pre_pause_state=pre_pause_state,
                ttl_deadline=ttl_deadline
            )
            
            if success:
                # Transition succeeded, fetch updated state
                final_state = self.fsm_store.get_state(agent_name)
                return (True, final_state)
            
            # Version conflict, retry with updated version
            logger.info(f"CAS retry {attempt + 1}/{self.MAX_CAS_RETRIES} for agent {agent_name}")
            current_version = new_version
        
        # Max retries exceeded
        logger.error(f"CAS retries exhausted for agent {agent_name} (transition to {target_state.value})")
        final_state = self.fsm_store.get_state(agent_name)
        return (False, final_state)
    
    def accept_command(
        self,
        agent_name: str,
        command_id: UUID,
        issued_at: datetime,
        ttl_ms: int,
        idempotency_key: Optional[str] = None
    ) -> Tuple[CommandGuardResult, Optional[AgentFSMSnapshot]]:
        """
        Full command acceptance flow: guards → transition to acknowledging.
        
        Returns:
            (result, state_after_transition)
            - result: PASSED if command accepted, error code otherwise
            - state_after_transition: FSM state after transition (or current if failed)
        """
        # 1. Check guards
        guard_result, current_state = self.check_command_guards(
            agent_name, command_id, issued_at, ttl_ms, idempotency_key
        )
        
        if guard_result != CommandGuardResult.PASSED:
            return (guard_result, current_state)
        
        # 2. Transition idle → acknowledging
        ttl_deadline = issued_at + timedelta(milliseconds=ttl_ms) if ttl_ms > 0 else None
        
        success, new_state = self.transition_with_retry(
            agent_name=agent_name,
            expected_version=current_state.version,
            target_state=FSMState.ACKNOWLEDGING,
            command_id=command_id,
            ttl_deadline=ttl_deadline
        )
        
        if not success:
            return (CommandGuardResult.VERSION_CONFLICT, new_state)
        
        logger.info(f"Command {command_id} accepted by agent {agent_name} (v{current_state.version} → v{new_state.version})")
        return (CommandGuardResult.PASSED, new_state)
    
    def mark_working(self, agent_name: str) -> Tuple[bool, AgentFSMSnapshot]:
        """Transition acknowledging → working (after ack published)"""
        state = self.fsm_store.get_state(agent_name)
        
        if not state or state.state != FSMState.ACKNOWLEDGING:
            logger.error(f"Cannot mark {agent_name} as working: not in acknowledging state (current: {state.state.value if state else 'None'})")
            return (False, state)
        
        return self.transition_with_retry(
            agent_name=agent_name,
            expected_version=state.version,
            target_state=FSMState.WORKING,
            command_id=state.command_id,
            ttl_deadline=state.ttl_deadline
        )
    
    def mark_completed(self, agent_name: str) -> Tuple[bool, AgentFSMSnapshot]:
        """Transition working → idle (after result published)"""
        state = self.fsm_store.get_state(agent_name)
        
        if not state or state.state != FSMState.WORKING:
            logger.error(f"Cannot mark {agent_name} as completed: not in working state (current: {state.state.value if state else 'None'})")
            return (False, state)
        
        return self.transition_with_retry(
            agent_name=agent_name,
            expected_version=state.version,
            target_state=FSMState.IDLE,
            command_id=None,  # Clear command_id when returning to idle
            ttl_deadline=None
        )
    
    def mark_failed(self, agent_name: str) -> Tuple[bool, AgentFSMSnapshot]:
        """Transition working/blocked → error (after error published)"""
        state = self.fsm_store.get_state(agent_name)
        
        if not state or state.state not in [FSMState.WORKING, FSMState.BLOCKED]:
            logger.error(f"Cannot mark {agent_name} as failed: not in working/blocked state (current: {state.state.value if state else 'None'})")
            return (False, state)
        
        return self.transition_with_retry(
            agent_name=agent_name,
            expected_version=state.version,
            target_state=FSMState.ERROR,
            command_id=state.command_id,  # Keep command_id to track which command failed
            ttl_deadline=state.ttl_deadline
        )
    
    def pause(self, agent_name: str) -> Tuple[bool, AgentFSMSnapshot]:
        """Pause agent (stores pre-pause state for resume)"""
        state = self.fsm_store.get_state(agent_name)
        
        if not state:
            logger.error(f"Cannot pause {agent_name}: FSM not initialized")
            return (False, None)
        
        if state.state == FSMState.PAUSED:
            logger.info(f"Agent {agent_name} already paused")
            return (True, state)
        
        return self.transition_with_retry(
            agent_name=agent_name,
            expected_version=state.version,
            target_state=FSMState.PAUSED,
            command_id=state.command_id,
            pre_pause_state=state.state,  # Store current state
            ttl_deadline=state.ttl_deadline
        )
    
    def resume(self, agent_name: str) -> Tuple[bool, AgentFSMSnapshot]:
        """Resume agent (restores pre-pause state)"""
        state = self.fsm_store.get_state(agent_name)
        
        if not state or state.state != FSMState.PAUSED:
            logger.error(f"Cannot resume {agent_name}: not paused (current: {state.state.value if state else 'None'})")
            return (False, state)
        
        if not state.pre_pause_state:
            logger.warning(f"Agent {agent_name} paused but no pre_pause_state stored, defaulting to idle")
            target_state = FSMState.IDLE
        else:
            target_state = state.pre_pause_state
        
        return self.transition_with_retry(
            agent_name=agent_name,
            expected_version=state.version,
            target_state=target_state,
            command_id=state.command_id,
            pre_pause_state=None,  # Clear pre_pause_state
            ttl_deadline=state.ttl_deadline
        )
    
    def reset(self, agent_name: str) -> AgentFSMSnapshot:
        """Force reset agent to idle (for manual recovery, bypasses CAS)"""
        return self.fsm_store.reset_to_idle(agent_name)
