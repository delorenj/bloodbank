"""
Idempotency key management for command deduplication.

Redis schema:
  Key: agent:{name}:idemp:{idempotency_key}
  Type: STRING (value = command_id)
  TTL: 300s (5 minute dedup window)

If a command arrives with an idempotency key that already exists in Redis,
the command is not re-executed. Instead, a result with outcome="skipped" is returned.
"""

import redis
from typing import Optional
from uuid import UUID
import logging

logger = logging.getLogger(__name__)


class IdempotencyStore:
    """Redis-backed idempotency key tracking"""
    
    DEDUP_WINDOW_SECONDS = 300  # 5 minutes
    
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
    
    def _key(self, agent_name: str, idempotency_key: str) -> str:
        """Generate Redis key for idempotency tracking"""
        return f"agent:{agent_name}:idemp:{idempotency_key}"
    
    def check_and_record(self, agent_name: str, idempotency_key: str, command_id: UUID) -> bool:
        """
        Check if idempotency key has been seen, and record it if not.
        
        Returns:
            True: Key is NEW (command should be executed)
            False: Key already EXISTS (command is a duplicate, skip execution)
        """
        key = self._key(agent_name, idempotency_key)
        
        # SET NX (set if not exists) with TTL
        # Returns 1 if key was set (new), 0 if key already existed (duplicate)
        result = self.redis.set(key, str(command_id), ex=self.DEDUP_WINDOW_SECONDS, nx=True)
        
        if result:
            logger.debug(f"Idempotency key {idempotency_key} recorded for agent {agent_name} (command {command_id})")
            return True  # New key, proceed with execution
        else:
            existing_command_id = self.redis.get(key)
            logger.info(f"Idempotency key {idempotency_key} already seen for agent {agent_name} (original command: {existing_command_id.decode('utf-8') if existing_command_id else 'unknown'})")
            return False  # Duplicate, skip execution
    
    def get_original_command(self, agent_name: str, idempotency_key: str) -> Optional[UUID]:
        """
        Get the original command ID for a given idempotency key.
        Returns None if key not found (expired or never existed).
        """
        key = self._key(agent_name, idempotency_key)
        value = self.redis.get(key)
        
        if value:
            return UUID(value.decode('utf-8'))
        return None
    
    def remove(self, agent_name: str, idempotency_key: str) -> bool:
        """
        Manually remove an idempotency key (for debugging/recovery).
        Returns True if key was deleted, False if it didn't exist.
        """
        key = self._key(agent_name, idempotency_key)
        deleted = self.redis.delete(key)
        
        if deleted:
            logger.warning(f"Manually removed idempotency key {idempotency_key} for agent {agent_name}")
        
        return bool(deleted)
    
    def get_ttl(self, agent_name: str, idempotency_key: str) -> int:
        """
        Get remaining TTL for an idempotency key in seconds.
        Returns -2 if key doesn't exist, -1 if key has no expiry (shouldn't happen).
        """
        key = self._key(agent_name, idempotency_key)
        return self.redis.ttl(key)
