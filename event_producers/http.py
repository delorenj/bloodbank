from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from uuid import UUID, uuid4
import logging
import socket

from event_producers.config import settings
from event_producers.rabbit import Publisher
from event_producers.events.base import EventEnvelope, Source, TriggerType, create_envelope
from event_producers.events.core.abstraction import BaseEvent
from event_producers.events.domains.agent.thread import AgentThreadPrompt, AgentThreadResponse
from event_producers.events.domains.claude_code import (
    SessionAgentToolAction,
    SessionThreadEnd,
    SessionThreadStart,
    SessionThreadMessage,
    SessionThreadError,
    ThinkingEvent,
)
from event_producers.events.registry import get_registry

logger = logging.getLogger(__name__)

app = FastAPI(title="bloodbank", version="0.2.0")

publisher = Publisher(enable_correlation_tracking=True)


@app.on_event("startup")
async def _startup():
    await publisher.start()
    # Ensure registry is populated
    get_registry().auto_discover_domains()


@app.on_event("shutdown")
async def _shutdown():
    await publisher.close()


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": settings.service_name}


async def publish_event_object(event: BaseEvent, source: Source = None):
    """
    Idiomatic OOP publish method.
    Takes a strongly-typed Event (or Command) object, wraps it, and publishes it.
    """
    # 1. Determine Routing Key
    # We need to look up the routing key for this class from the Registry or class metadata
    registry = get_registry()
    
    # We iterate domains to find the key for this class
    # Optimally, the class would know its own key (e.g. via a class var or method)
    # But currently it's in ROUTING_KEYS dict in the module.
    # We can reverse lookup in registry.
    
    routing_key = None
    for domain in registry.domains.values():
        for key, cls in domain.payload_types.items():
            if isinstance(event, cls):
                routing_key = key
                break
        if routing_key:
            break
            
    if not routing_key:
        raise ValueError(f"Could not determine routing key for event type: {type(event)}")

    # 2. Create Envelope
    if not source:
        source = Source(
            host=socket.gethostname(),
            type=TriggerType.MANUAL,
            app="bloodbank-http"
        )

    envelope = create_envelope(
        event_type=routing_key,
        payload=event,
        source=source,
        event_id=uuid4()
    )

    # 3. Publish
    await publisher.publish(
        routing_key=routing_key,
        body=envelope.model_dump(),
        event_id=envelope.event_id
    )
    return envelope


# --- publish endpoints ---

@app.post("/events/custom")
async def publish_custom_event(envelope: dict):
    """
    Generic endpoint to publish any event envelope.
    """
    try:
        event_type = envelope.get("event_type")
        event_id = envelope.get("event_id")
        
        if not event_type:
            raise HTTPException(status_code=400, detail="Missing event_type")

        await publisher.publish(
            routing_key=event_type,
            body=envelope, # Use body param name correctly
            event_id=UUID(event_id) if event_id else None
        )
        return JSONResponse({"status": "published", "event_id": event_id})
        
    except Exception as e:
        logger.error(f"Error publishing custom event: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/events/agent/thread/prompt")
async def publish_prompt(ev: AgentThreadPrompt, request: Request):
    try:
        client_host = request.client.host if request.client else "unknown"
        source = Source(host=client_host, type=TriggerType.MANUAL, app="http-client")
        
        # Use new generic publish method
        envelope = await publish_event_object(ev, source)
        
        return JSONResponse(envelope.model_dump())
    except Exception as e:
        logger.error(f"Failed to publish prompt: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================================
# Claude Code Events API
# ============================================================================

@app.post("/events/claude-code/tool-action")
async def publish_tool_action(ev: SessionAgentToolAction, request: Request):
    """
    Publish Claude Code tool usage event.

    Endpoint: POST /events/claude-code/tool-action
    Event Type: session.thread.agent.action
    """
    try:
        client_host = request.client.host if request.client else "unknown"
        source = Source(host=client_host, type=TriggerType.AGENT, app="claude-code")

        envelope = await publish_event_object(ev, source)
        return JSONResponse(envelope.model_dump())
    except Exception as e:
        logger.error(f"Failed to publish tool action: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/events/claude-code/session-start")
async def publish_session_start(ev: SessionThreadStart, request: Request):
    """
    Publish Claude Code session start event.

    Endpoint: POST /events/claude-code/session-start
    Event Type: session.thread.start
    """
    try:
        client_host = request.client.host if request.client else "unknown"
        source = Source(host=client_host, type=TriggerType.AGENT, app="claude-code")

        envelope = await publish_event_object(ev, source)
        return JSONResponse(envelope.model_dump())
    except Exception as e:
        logger.error(f"Failed to publish session start: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/events/claude-code/session-end")
async def publish_session_end(ev: SessionThreadEnd, request: Request):
    """
    Publish Claude Code session end event.

    Endpoint: POST /events/claude-code/session-end
    Event Type: session.thread.end
    """
    try:
        client_host = request.client.host if request.client else "unknown"
        source = Source(host=client_host, type=TriggerType.AGENT, app="claude-code")

        envelope = await publish_event_object(ev, source)
        return JSONResponse(envelope.model_dump())
    except Exception as e:
        logger.error(f"Failed to publish session end: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/events/claude-code/message")
async def publish_session_message(ev: SessionThreadMessage, request: Request):
    """
    Publish Claude Code message event.

    Endpoint: POST /events/claude-code/message
    Event Type: session.thread.message
    """
    try:
        client_host = request.client.host if request.client else "unknown"
        source = Source(host=client_host, type=TriggerType.AGENT, app="claude-code")

        envelope = await publish_event_object(ev, source)
        return JSONResponse(envelope.model_dump())
    except Exception as e:
        logger.error(f"Failed to publish session message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/events/claude-code/error")
async def publish_session_error(ev: SessionThreadError, request: Request):
    """
    Publish Claude Code error event.

    Endpoint: POST /events/claude-code/error
    Event Type: session.thread.error
    """
    try:
        client_host = request.client.host if request.client else "unknown"
        source = Source(host=client_host, type=TriggerType.AGENT, app="claude-code")

        envelope = await publish_event_object(ev, source)
        return JSONResponse(envelope.model_dump())
    except Exception as e:
        logger.error(f"Failed to publish session error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/events/claude-code/thinking")
async def publish_thinking_event(ev: ThinkingEvent, request: Request):
    """
    Publish Claude Code thinking/reasoning event.

    Endpoint: POST /events/claude-code/thinking
    Event Type: session.thread.agent.thinking
    """
    try:
        client_host = request.client.host if request.client else "unknown"
        source = Source(host=client_host, type=TriggerType.AGENT, app="claude-code")

        envelope = await publish_event_object(ev, source)
        return JSONResponse(envelope.model_dump())
    except Exception as e:
        logger.error(f"Failed to publish thinking event: {e}")
        raise HTTPException(status_code=500, detail=str(e))
