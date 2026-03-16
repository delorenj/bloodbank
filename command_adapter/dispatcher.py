"""
Pluggable dispatcher system for command execution.

Provides a registry-based dispatch mechanism that routes commands to
appropriate execution backends (OpenClaw hooks, HTTP endpoints, etc.).

Architecture:
    CommandAdapter → DispatcherRegistry → Dispatcher (OpenClaw/HTTP/etc.)

This replaces the hardwired OpenClawHookDispatcher with a pluggable system
that can route different agents to different execution backends.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    """Result of a command dispatch to any backend."""

    success: bool
    status_code: int
    response_body: dict[str, Any] | None = None
    error: str | None = None
    backend: str = "unknown"  # Which dispatcher handled this


@runtime_checkable
class Dispatcher(Protocol):
    """Protocol for command dispatchers.

    Any dispatcher must implement this interface to be registered
    with the DispatcherRegistry.
    """

    @property
    def name(self) -> str:
        """Unique identifier for this dispatcher type."""
        ...

    async def dispatch(
        self,
        *,
        target_agent: str,
        action: str,
        command_id: str,
        issued_by: str,
        priority: str,
        command_payload: dict[str, Any] | None,
    ) -> DispatchResult:
        """Execute a command for the target agent.

        Args:
            target_agent: Agent name to dispatch to
            action: Action to perform
            command_id: Unique command identifier
            issued_by: Who issued the command
            priority: Command priority (normal, high, critical)
            command_payload: Optional payload for the command

        Returns:
            DispatchResult with success/failure status
        """
        ...


@dataclass
class DispatcherRegistry:
    """Registry for pluggable command dispatchers.

    Maps agent names (or patterns) to dispatcher instances.
    Supports:
    - Exact agent name matching: "cack" → OpenClawDispatcher
    - Pattern matching: "external-*" → HTTPDispatcher
    - Default fallback for unmatched agents

    Configuration via environment:
        DISPATCHER_DEFAULT=openclaw|http
        DISPATCHER_<AGENT>=openclaw|http|<custom>
        HTTP_DISPATCHER_<AGENT>_URL=https://...

    Example:
        registry = DispatcherRegistry()
        registry.register("cack", openclaw_dispatcher)
        registry.register("external-*", http_dispatcher)

        dispatcher = registry.get_dispatcher("cack")
        result = await dispatcher.dispatch(...)
    """

    _dispatchers: dict[str, Dispatcher] = field(default_factory=dict)
    _patterns: list[tuple[str, Dispatcher]] = field(default_factory=list)
    _default: Dispatcher | None = None

    def register(self, agent: str, dispatcher: Dispatcher) -> None:
        """Register a dispatcher for an agent name or pattern.

        Args:
            agent: Exact agent name or pattern (e.g., "external-*")
            dispatcher: Dispatcher instance to handle this agent
        """
        if "*" in agent:
            self._patterns.append((agent, dispatcher))
            logger.info(f"Registered pattern dispatcher: {agent} → {dispatcher.name}")
        else:
            self._dispatchers[agent] = dispatcher
            logger.info(f"Registered agent dispatcher: {agent} → {dispatcher.name}")

    def set_default(self, dispatcher: Dispatcher) -> None:
        """Set the default dispatcher for unmatched agents."""
        self._default = dispatcher
        logger.info(f"Set default dispatcher: {dispatcher.name}")

    def get_dispatcher(self, agent: str) -> Dispatcher | None:
        """Get the dispatcher for an agent name.

        Resolution order:
        1. Exact match in _dispatchers
        2. Pattern match in _patterns (first match wins)
        3. Default dispatcher

        Args:
            agent: Agent name to look up

        Returns:
            Dispatcher instance or None if no match
        """
        # 1. Exact match
        if agent in self._dispatchers:
            return self._dispatchers[agent]

        # 2. Pattern match
        for pattern, dispatcher in self._patterns:
            if self._match_pattern(pattern, agent):
                return dispatcher

        # 3. Default
        return self._default

    def _match_pattern(self, pattern: str, agent: str) -> bool:
        """Match an agent name against a pattern.

        Supports:
        - "prefix-*" → matches any agent starting with "prefix-"
        - "*-suffix" → matches any agent ending with "-suffix"
        - "*" → matches all agents
        """
        if pattern == "*":
            return True

        if pattern.endswith("-*"):
            prefix = pattern[:-2]
            return agent.startswith(prefix + "-") or agent == prefix

        if pattern.startswith("*-"):
            suffix = pattern[2:]
            return agent.endswith("-" + suffix) or agent == suffix

        return False

    def list_agents(self) -> list[str]:
        """List all registered agent names (not patterns)."""
        return list(self._dispatchers.keys())

    def list_patterns(self) -> list[str]:
        """List all registered patterns."""
        return [p for p, _ in self._patterns]


def build_registry_from_env(
    openclaw_dispatcher: Dispatcher,
    http_dispatcher_factory: Callable[[str], Dispatcher],
) -> DispatcherRegistry:
    """Build a DispatcherRegistry from environment configuration.

    Environment variables:
        DISPATCHER_DEFAULT: Default dispatcher type (openclaw, http)
        DISPATCHER_<AGENT>: Dispatcher type for specific agent
        HTTP_DISPATCHER_<AGENT>_URL: HTTP endpoint for HTTP dispatcher

    Args:
        openclaw_dispatcher: Pre-configured OpenClaw dispatcher instance
        http_dispatcher_factory: Factory function to create HTTP dispatchers

    Returns:
        Configured DispatcherRegistry
    """
    import os

    registry = DispatcherRegistry()

    # Get default dispatcher type
    default_type = os.environ.get("DISPATCHER_DEFAULT", "openclaw").lower()
    if default_type == "openclaw":
        registry.set_default(openclaw_dispatcher)
    elif default_type == "http":
        # For HTTP default, we need a base URL
        default_url = os.environ.get("HTTP_DISPATCHER_DEFAULT_URL")
        if default_url:
            registry.set_default(http_dispatcher_factory(default_url))
        else:
            logger.warning("HTTP default dispatcher requested but no URL configured")
            registry.set_default(openclaw_dispatcher)

    # Parse agent-specific dispatchers
    # Look for DISPATCHER_<AGENT> env vars
    for key, value in os.environ.items():
        if key.startswith("DISPATCHER_") and key != "DISPATCHER_DEFAULT":
            # Extract agent name from DISPATCHER_<AGENT>
            agent = key[len("DISPATCHER_") :].lower().replace("_", "-")
            dispatcher_type = value.lower()

            if dispatcher_type == "openclaw":
                registry.register(agent, openclaw_dispatcher)
            elif dispatcher_type == "http":
                # Look for HTTP_DISPATCHER_<AGENT>_URL
                url_key = f"HTTP_DISPATCHER_{key[len('DISPATCHER_') :]}_URL"
                url = os.environ.get(url_key)
                if url:
                    registry.register(agent, http_dispatcher_factory(url))
                else:
                    logger.warning(
                        f"HTTP dispatcher for {agent} requested but no URL found"
                    )
            else:
                logger.warning(
                    f"Unknown dispatcher type for {agent}: {dispatcher_type}"
                )

    return registry
