from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from typing import Optional
from uuid import UUID
import logging

from .events import AgentThreadPrompt, AgentThreadResponse
from rabbit import Publisher
from config import settings

logger = logging.getLogger(__name__)

app = FastAPI(title="bloodbank", version="0.2.0")

publisher = Publisher(enable_correlation_tracking=True)


@app.on_event("startup")
async def _startup():
    await publisher.start()


@app.on_event("shutdown")
async def _shutdown():
    await publisher.close()


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": settings.service_name}


# --- publish endpoints ---


@app.post("/events/agent/thread/prompt")
async def publish_prompt(ev: AgentThreadPrompt, request: Request):
    env = envelope_for(
        "agent.thread.prompt", source="http/" + request.client.host, data=ev
    )
    await publisher.publish("agent.thread.prompt", env.model_dump(), message_id=env.id)
    return JSONResponse(env.model_dump())


@app.post("/events/agent/thread/response")
async def publish_response(ev: AgentThreadResponse, request: Request):
    env = envelope_for(
        "agent.thread.response",
        source="http/" + request.client.host,
        data=ev,
        correlation_id=ev.prompt_id,
    )
    await publisher.publish(
        "agent.thread.response",
        env.model_dump(),
        message_id=env.id,
        correlation_id=env.correlation_id,
    )
    return JSONResponse(env.model_dump())


# --- Debug Endpoints for Correlation Tracking ---


@app.get("/debug/correlation/{event_id}")
async def debug_correlation(event_id: str):
    """
    Get full correlation debug information for an event.

    Returns parents, children, ancestors, descendants, and metadata.
    """
    if not publisher.enable_correlation_tracking:
        raise HTTPException(
            status_code=503,
            detail="Correlation tracking is not enabled. Initialize Publisher with enable_correlation_tracking=True",
        )

    try:
        event_uuid = UUID(event_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid UUID format: {event_id}")

    try:
        debug_data = await publisher.debug_correlation(event_uuid)

        # Check if event exists (has any correlation data)
        if not any(
            [
                debug_data.get("parents"),
                debug_data.get("children"),
                debug_data.get("metadata"),
            ]
        ):
            raise HTTPException(
                status_code=404,
                detail=f"No correlation data found for event {event_id}",
            )

        return debug_data

    except Exception as e:
        logger.error(f"Error retrieving correlation debug data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug/correlation/{event_id}/chain")
async def get_correlation_chain(event_id: str, direction: str = "ancestors"):
    """
    Get correlation chain for an event.

    Args:
        event_id: UUID of the event
        direction: "ancestors" (default) or "descendants"

    Returns:
        List of event UUIDs in the chain
    """
    if not publisher.enable_correlation_tracking:
        raise HTTPException(
            status_code=503,
            detail="Correlation tracking is not enabled. Initialize Publisher with enable_correlation_tracking=True",
        )

    if direction not in ["ancestors", "descendants"]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid direction: {direction}. Must be 'ancestors' or 'descendants'",
        )

    try:
        event_uuid = UUID(event_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid UUID format: {event_id}")

    try:
        chain = await publisher.get_correlation_chain(event_uuid, direction)

        if not chain:
            raise HTTPException(
                status_code=404,
                detail=f"No correlation chain found for event {event_id}",
            )

        return {
            "event_id": event_id,
            "direction": direction,
            "chain": [str(uuid) for uuid in chain],
            "count": len(chain),
        }

    except Exception as e:
        logger.error(f"Error retrieving correlation chain: {e}")
        raise HTTPException(status_code=500, detail=str(e))
