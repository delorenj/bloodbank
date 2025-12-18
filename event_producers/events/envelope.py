"""
Unified event envelope creation and management.

This module provides a single, consistent interface for creating event envelopes
across the entire bloodbank application. It replaces the fragmented envelope_for
functions with a unified, type-safe API.
"""

from datetime import datetime, timezone
from typing import Any, List, Optional, Union
from uuid import UUID, uuid4

from .base import (
    EventEnvelope,
    Source,
    TriggerType,
    AgentContext,
    AgentType,
    CodeState,
)


def create_source(
    host: str,
    trigger_type: Union[str, TriggerType],
    app: Optional[str] = None,
    meta: Optional[dict] = None,
) -> Source:
    """
    Create a Source object with proper type conversion.
    
    Args:
        host: Machine that generated the event
        trigger_type: How the event was triggered (string or TriggerType enum)
        app: Application name
        meta: Additional context metadata
        
    Returns:
        Source object with proper typing
    """
    if isinstance(trigger_type, str):
        try:
            trigger_type = TriggerType(trigger_type)
        except ValueError:
            trigger_type = TriggerType.MANUAL  # Fallback to manual
            
    return Source(
        host=host,
        type=trigger_type,
        app=app,
        meta=meta or {}
    )


def create_agent_context(
    agent_type: Union[str, AgentType],
    name: Optional[str] = None,
    system_prompt: Optional[str] = None,
    instance_id: Optional[str] = None,
    mcp_servers: Optional[List[str]] = None,
    file_references: Optional[List[str]] = None,
    url_references: Optional[List[str]] = None,
    code_state: Optional[CodeState] = None,
    checkpoint_id: Optional[str] = None,
    meta: Optional[dict] = None,
) -> AgentContext:
    """
    Create an AgentContext object with proper type conversion.
    
    Args:
        agent_type: Type of agent (string or AgentType enum)
        name: Agent's persona/name
        system_prompt: Initial system prompt
        instance_id: Unique session identifier
        mcp_servers: Connected MCP servers
        file_references: Files in context
        url_references: URLs in context
        code_state: Git state snapshot
        checkpoint_id: For checkpoint-based agents
        meta: Additional metadata
        
    Returns:
        AgentContext object with proper typing
    """
    if isinstance(agent_type, str):
        try:
            agent_type = AgentType(agent_type)
        except ValueError:
            agent_type = AgentType.CUSTOM  # Fallback to custom
            
    return AgentContext(
        type=agent_type,
        name=name,
        system_prompt=system_prompt,
        instance_id=instance_id,
        mcp_servers=mcp_servers or [],
        file_references=file_references or [],
        url_references=url_references or [],
        code_state=code_state,
        checkpoint_id=checkpoint_id,
        meta=meta or {}
    )


def create_envelope(
    event_type: str,
    payload: Any,
    source: Union[Source, str],  # Allow Source object or simple string
    correlation_ids: Optional[List[Union[UUID, str]]] = None,
    agent_context: Optional[AgentContext] = None,
    event_id: Optional[UUID] = None,
) -> EventEnvelope:
    """
    Create a properly-formed event envelope with flexible source handling.
    
    This is the primary envelope creation function for the entire application.
    It handles both simple string sources and full Source objects for backward
    compatibility and ease of use.
    
    Args:
        event_type: Routing key (e.g., "fireflies.transcript.ready")
        payload: Your typed payload
        source: Source object or simple string (e.g., "http/127.0.0.1")
        correlation_ids: List of parent event IDs (UUIDs or strings)
        agent_context: Agent metadata (if source.type == AGENT)
        event_id: Optional explicit event ID (for deterministic IDs)
        
    Returns:
        EventEnvelope with proper typing
        
    Examples:
        >>> # Simple usage with string source
        >>> envelope = create_envelope(
        ...     event_type="test.event",
        ...     payload={"data": "test"},
        ...     source="http/127.0.0.1"
        ... )
        
        >>> # Advanced usage with full Source and AgentContext
        >>> source = create_source(
        ...     host="localhost",
        ...     trigger_type=TriggerType.AGENT,
        ...     app="my-app"
        ... )
        >>> agent_ctx = create_agent_context(
        ...     agent_type=AgentType.CLAUDE_CODE,
        ...     name="Code Assistant"
        ... )
        >>> envelope = create_envelope(
        ...     event_type="code.completion",
        ...     payload=completion_data,
        ...     source=source,
        ...     agent_context=agent_ctx,
        ...     correlation_ids=[parent_event_id]
        ... )
    """
    # Handle source parameter - convert string to Source if needed
    if isinstance(source, str):
        if "/" in source:
            # Format like "http/127.0.0.1" -> app/host
            parts = source.split("/", 1)
            app, host = parts[0], parts[1]
            source_obj = create_source(
                host=host,
                trigger_type=TriggerType.MANUAL,
                app=app
            )
        else:
            # Simple string, treat as app name
            source_obj = create_source(
                host="unknown",
                trigger_type=TriggerType.MANUAL,
                app=source
            )
    else:
        source_obj = source
        
    # Convert correlation IDs to UUIDs if they're strings
    if correlation_ids:
        correlation_uuids = []
        for corr_id in correlation_ids:
            if isinstance(corr_id, str):
                try:
                    correlation_uuids.append(UUID(corr_id))
                except ValueError:
                    # Skip invalid UUIDs
                    continue
            else:
                correlation_uuids.append(corr_id)
    else:
        correlation_uuids = []
        
    return EventEnvelope(
        event_id=event_id or uuid4(),
        event_type=event_type,
        timestamp=datetime.now(timezone.utc),
        version="1.0.0",
        source=source_obj,
        correlation_ids=correlation_uuids,
        agent_context=agent_context,
        payload=payload,
    )


# Backward compatibility alias - DEPRECATED
def envelope_for(event_type: str, source: str, data: Any, correlation_id: Optional[Union[UUID, str]] = None):
    """
    DEPRECATED: Use create_envelope() instead.
    
    This function exists for backward compatibility with old code.
    It will be removed in a future version.
    
    Args:
        event_type: Event type/routing key
        source: Source string (e.g., "http/bloodbank")
        data: Event payload
        correlation_id: Single correlation ID (deprecated, use correlation_ids in create_envelope)
        
    Returns:
        EventEnvelope
    """
    import warnings
    warnings.warn(
        "envelope_for() is deprecated. Use create_envelope() instead.",
        DeprecationWarning,
        stacklevel=2
    )
    
    correlation_ids = [correlation_id] if correlation_id else []
    
    return create_envelope(
        event_type=event_type,
        payload=data,
        source=source,
        correlation_ids=correlation_ids
    )


# Convenience functions for common patterns

def create_http_envelope(
    event_type: str,
    payload: Any,
    client_host: str,
    app_name: str = "http",
    **kwargs
) -> EventEnvelope:
    """
    Create an envelope for HTTP-triggered events.
    
    Args:
        event_type: Event type/routing key
        payload: Event payload
        client_host: Client IP address or hostname
        app_name: Application name (default: "http")
        **kwargs: Additional arguments passed to create_envelope
        
    Returns:
        EventEnvelope configured for HTTP source
    """
    source = create_source(
        host=client_host,
        trigger_type=TriggerType.MANUAL,
        app=app_name
    )
    
    return create_envelope(
        event_type=event_type,
        payload=payload,
        source=source,
        **kwargs
    )


def create_agent_envelope(
    event_type: str,
    payload: Any,
    agent_type: Union[str, AgentType],
    agent_name: Optional[str] = None,
    host: str = "localhost",
    **kwargs
) -> EventEnvelope:
    """
    Create an envelope for agent-triggered events.
    
    Args:
        event_type: Event type/routing key
        payload: Event payload
        agent_type: Type of agent
        agent_name: Agent's name/persona
        host: Host where agent is running
        **kwargs: Additional arguments passed to create_envelope
        
    Returns:
        EventEnvelope configured for agent source
    """
    source = create_source(
        host=host,
        trigger_type=TriggerType.AGENT,
        app="agent"
    )
    
    agent_context = create_agent_context(
        agent_type=agent_type,
        name=agent_name
    )
    
    return create_envelope(
        event_type=event_type,
        payload=payload,
        source=source,
        agent_context=agent_context,
        **kwargs
    )


def create_scheduled_envelope(
    event_type: str,
    payload: Any,
    app_name: str = "scheduler",
    host: str = "localhost",
    **kwargs
) -> EventEnvelope:
    """
    Create an envelope for scheduled/cron-triggered events.
    
    Args:
        event_type: Event type/routing key
        payload: Event payload
        app_name: Application name (default: "scheduler")
        host: Host where scheduler is running
        **kwargs: Additional arguments passed to create_envelope
        
    Returns:
        EventEnvelope configured for scheduled source
    """
    source = create_source(
        host=host,
        trigger_type=TriggerType.SCHEDULED,
        app=app_name
    )
    
    return create_envelope(
        event_type=event_type,
        payload=payload,
        source=source,
        **kwargs
    )
