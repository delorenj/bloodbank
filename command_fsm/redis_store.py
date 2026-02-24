"""
Redis storage layer for agent FSM state with atomic CAS transitions.

Redis schema:
  Key: agent:{name}:fsm
  Type: HASH
  Fields:
    state           = "idle" | "acknowledging" | "working" | "blocked" | "error" | "paused"
    version         = integer (monotonic, for optimistic concurrency)
    command_id      = uuid string (current command, null if idle)
    entered_at      = ISO 8601 timestamp
    pre_pause_state = state before pause (null if not paused)
    ttl_deadline    = ISO 8601 timestamp (command expiry, null if no TTL)

Lua CAS script ensures atomic read-modify-write with version checking.
"""

import redis
from typing import Optional
from datetime import datetime, timezone
from uuid import UUID
import logging

from .states import FSMState, AgentFSMSnapshot

logger = logging.getLogger(__name__)


# Lua script for atomic compare-and-swap transition
TRANSITION_SCRIPT = """
local key = KEYS[1]
local expected_version = tonumber(ARGV[1])
local new_state = ARGV[2]
local new_command_id = ARGV[3]
local entered_at = ARGV[4]
local pre_pause_state = ARGV[5]
local ttl_deadline = ARGV[6]

-- Read current version
local current_version = tonumber(redis.call('HGET', key, 'version') or '0')

-- Version conflict check
if current_version ~= expected_version then
    return {0, current_version}  -- {success=false, actual_version}
end

-- Atomic update
local new_version = current_version + 1
redis.call('HMSET', key,
    'state', new_state,
    'version', new_version,
    'command_id', new_command_id,
    'entered_at', entered_at,
    'pre_pause_state', pre_pause_state,
    'ttl_deadline', ttl_deadline)

return {1, new_version}  -- {success=true, new_version}
"""


class RedisAgentFSMStore:
    """Redis-backed FSM state store with atomic transitions"""
    
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self._transition_script = None  # Lazy-loaded
    
    def _get_transition_script(self):
        """Load Lua script into Redis (cached)"""
        if not self._transition_script:
            self._transition_script = self.redis.register_script(TRANSITION_SCRIPT)
        return self._transition_script
    
    def _key(self, agent_name: str) -> str:
        """Generate Redis key for agent FSM state"""
        return f"agent:{agent_name}:fsm"
    
    def get_state(self, agent_name: str) -> Optional[AgentFSMSnapshot]:
        """
        Read current FSM state for an agent.
        Returns None if agent has no state (never initialized).
        """
        key = self._key(agent_name)
        data = self.redis.hgetall(key)
        
        if not data:
            return None
        
        # Decode bytes to strings
        data = {k.decode('utf-8') if isinstance(k, bytes) else k: 
                v.decode('utf-8') if isinstance(v, bytes) else v 
                for k, v in data.items()}
        
        return AgentFSMSnapshot(
            agent_name=agent_name,
            state=FSMState(data['state']),
            version=int(data['version']),
            command_id=UUID(data['command_id']) if data.get('command_id') and data['command_id'] != 'null' else None,
            entered_at=datetime.fromisoformat(data['entered_at']),
            pre_pause_state=FSMState(data['pre_pause_state']) if data.get('pre_pause_state') and data['pre_pause_state'] != 'null' else None,
            ttl_deadline=datetime.fromisoformat(data['ttl_deadline']) if data.get('ttl_deadline') and data['ttl_deadline'] != 'null' else None
        )
    
    def initialize_state(self, agent_name: str) -> AgentFSMSnapshot:
        """
        Initialize FSM state for a new agent (always starts in idle).
        If state already exists, returns the existing state without modification.
        """
        existing = self.get_state(agent_name)
        if existing:
            logger.info(f"Agent {agent_name} FSM already initialized at version {existing.version}")
            return existing
        
        key = self._key(agent_name)
        now = datetime.now(timezone.utc).isoformat()
        
        self.redis.hset(key, mapping={
            'state': FSMState.IDLE.value,
            'version': 1,
            'command_id': 'null',
            'entered_at': now,
            'pre_pause_state': 'null',
            'ttl_deadline': 'null'
        })
        
        logger.info(f"Initialized FSM for agent {agent_name} (state=idle, version=1)")
        
        return AgentFSMSnapshot(
            agent_name=agent_name,
            state=FSMState.IDLE,
            version=1,
            command_id=None,
            entered_at=datetime.now(timezone.utc),
            pre_pause_state=None,
            ttl_deadline=None
        )
    
    def transition(
        self,
        agent_name: str,
        expected_version: int,
        new_state: FSMState,
        command_id: Optional[UUID] = None,
        pre_pause_state: Optional[FSMState] = None,
        ttl_deadline: Optional[datetime] = None
    ) -> tuple[bool, int]:
        """
        Attempt atomic state transition with CAS.
        
        Returns:
            (success: bool, actual_version: int)
            - If success=True: transition succeeded, actual_version is the new version
            - If success=False: version conflict, actual_version is the current version in Redis
        """
        key = self._key(agent_name)
        script = self._get_transition_script()
        
        now = datetime.now(timezone.utc).isoformat()
        cmd_id_str = str(command_id) if command_id else 'null'
        pre_pause_str = pre_pause_state.value if pre_pause_state else 'null'
        ttl_str = ttl_deadline.isoformat() if ttl_deadline else 'null'
        
        result = script(
            keys=[key],
            args=[expected_version, new_state.value, cmd_id_str, now, pre_pause_str, ttl_str]
        )
        
        success = bool(result[0])
        actual_version = result[1]
        
        if success:
            logger.info(f"Agent {agent_name} FSM transition: → {new_state.value} (v{expected_version} → v{actual_version})")
        else:
            logger.warning(f"Agent {agent_name} FSM transition FAILED: version conflict (expected {expected_version}, got {actual_version})")
        
        return success, actual_version
    
    def reset_to_idle(self, agent_name: str) -> AgentFSMSnapshot:
        """
        Force reset agent to idle state (for manual recovery).
        Does NOT use CAS — unconditionally resets.
        """
        key = self._key(agent_name)
        now = datetime.now(timezone.utc).isoformat()
        
        # Increment version to invalidate any in-flight CAS attempts
        current = self.get_state(agent_name)
        new_version = (current.version + 1) if current else 1
        
        self.redis.hset(key, mapping={
            'state': FSMState.IDLE.value,
            'version': new_version,
            'command_id': 'null',
            'entered_at': now,
            'pre_pause_state': 'null',
            'ttl_deadline': 'null'
        })
        
        logger.warning(f"Force reset agent {agent_name} to idle (version {new_version})")
        
        return self.get_state(agent_name)
