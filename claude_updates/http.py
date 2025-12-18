"""
FastAPI HTTP server for publishing events to Bloodbank.

Provides REST endpoints for:
- Publishing LLM events
- Publishing artifact events
- Receiving webhooks from external services (Fireflies, etc.)
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import settings
from .events import (
    AIFilters,
    Artifact,
    FirefliesTranscriptFailedPayload,
    FirefliesTranscriptReadyPayload,
    FirefliesTranscriptUploadPayload,
    LLMErrorPayload,
    LLMPrompt,
    LLMResponse,
    SentimentType,
    Source,
    TranscriptSentence,
    TriggerType,
    create_envelope,
)
from .rabbit import Publisher

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


# ============================================================================
# LLM Events
# ============================================================================


@app.post("/events/llm/prompt")
async def publish_llm_prompt(payload: LLMPrompt, request: Request):
    """
    Publish LLM prompt event.

    Event Type: llm.prompt
    """
    source = Source(host=request.client.host, type=TriggerType.HOOK, app="http-api")

    # Generate deterministic event ID for idempotency
    event_id = publisher.generate_event_id(
        "llm.prompt",
        provider=payload.provider,
        prompt=payload.prompt[:100],  # First 100 chars
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    envelope = create_envelope(
        event_type="llm.prompt", payload=payload, source=source, event_id=event_id
    )

    await publisher.publish(
        routing_key="llm.prompt", body=envelope.model_dump(mode="json"), event_id=event_id
    )

    return JSONResponse({"event_id": str(envelope.event_id), "event_type": envelope.event_type})


@app.post("/events/llm/response")
async def publish_llm_response(payload: LLMResponse, request: Request):
    """
    Publish LLM response event.

    Event Type: llm.response
    Correlates to: llm.prompt (via correlation_ids)
    """
    source = Source(host=request.client.host, type=TriggerType.HOOK, app="http-api")

    # Extract prompt_id for backwards compatibility
    parent_event_ids = []
    if payload.prompt_id:
        try:
            parent_event_ids = [UUID(payload.prompt_id)]
        except (ValueError, AttributeError):
            pass

    event_id = uuid4()

    envelope = create_envelope(
        event_type="llm.response",
        payload=payload,
        source=source,
        correlation_ids=parent_event_ids,
        event_id=event_id,
    )

    await publisher.publish(
        routing_key="llm.response",
        body=envelope.model_dump(mode="json"),
        event_id=event_id,
        parent_event_ids=parent_event_ids,
    )

    return JSONResponse({"event_id": str(envelope.event_id), "event_type": envelope.event_type})


@app.post("/events/llm/error")
async def publish_llm_error(payload: LLMErrorPayload, request: Request):
    """
    Publish LLM error event.

    Event Type: llm.error
    """
    source = Source(host=request.client.host, type=TriggerType.HOOK, app="http-api")

    event_id = uuid4()

    envelope = create_envelope(
        event_type="llm.error", payload=payload, source=source, event_id=event_id
    )

    await publisher.publish(
        routing_key="llm.error", body=envelope.model_dump(mode="json"), event_id=event_id
    )

    return JSONResponse({"event_id": str(envelope.event_id), "event_type": envelope.event_type})


# ============================================================================
# Artifact Events
# ============================================================================


@app.post("/events/artifact")
async def publish_artifact(payload: Artifact, request: Request):
    """
    Publish artifact lifecycle event.

    Event Type: artifact.{action} (e.g., artifact.created, artifact.updated)
    """
    source = Source(host=request.client.host, type=TriggerType.HOOK, app="http-api")

    event_type = f"artifact.{payload.action}"

    # Generate deterministic event ID based on URI + action
    event_id = publisher.generate_event_id(event_type, uri=payload.uri, action=payload.action)

    envelope = create_envelope(
        event_type=event_type, payload=payload, source=source, event_id=event_id
    )

    await publisher.publish(
        routing_key=event_type, body=envelope.model_dump(mode="json"), event_id=event_id
    )

    return JSONResponse({"event_id": str(envelope.event_id), "event_type": envelope.event_type})


# ============================================================================
# Fireflies Webhooks
# ============================================================================


@app.post("/webhooks/fireflies/upload")
async def fireflies_upload_request(payload: FirefliesTranscriptUploadPayload, request: Request):
    """
    Request transcription upload to Fireflies.

    Event Type: fireflies.transcript.upload

    This creates the initial event in the chain. The event_id from this
    will be used as correlation_id in subsequent events.
    """
    source = Source(host=request.client.host, type=TriggerType.HOOK, app="http-api")

    # Generate deterministic event ID
    event_id = publisher.generate_event_id(
        "fireflies.transcript.upload",
        media_file=payload.media_file,
        user_id=payload.user_id or "unknown",
    )

    envelope = create_envelope(
        event_type="fireflies.transcript.upload", payload=payload, source=source, event_id=event_id
    )

    await publisher.publish(
        routing_key="fireflies.transcript.upload",
        body=envelope.model_dump(mode="json"),
        event_id=event_id,
    )

    return JSONResponse(
        {
            "event_id": str(envelope.event_id),
            "event_type": envelope.event_type,
            "message": "Upload request published. Use this event_id to track the correlation chain.",
        }
    )


@app.post("/webhooks/fireflies/ready")
async def fireflies_transcript_ready(req: Request):
    """
    Receive Fireflies transcription complete webhook.

    Event Type: fireflies.transcript.ready
    Correlates to: fireflies.transcript.upload (via correlation_ids)

    This webhook is called by Fireflies when transcription completes.
    We parse their payload and publish our standardized event.
    """
    body = await req.json()

    try:
        # Extract webhook data
        data = body.get("data", {}) if isinstance(body, dict) else body[0]["content"]["data"]

        # Parse sentences
        sentences = [
            TranscriptSentence(
                index=s["index"],
                speaker_name=s.get("speaker_name"),
                speaker_id=s["speaker_id"],
                raw_text=s["raw_text"],
                text=s["text"],
                start_time=s["start_time"],
                end_time=s["end_time"],
                ai_filters=AIFilters(
                    text_cleanup=s["ai_filters"]["text_cleanup"],
                    task=s["ai_filters"].get("task"),
                    pricing=s["ai_filters"].get("pricing"),
                    metric=s["ai_filters"].get("metric"),
                    question=s["ai_filters"].get("question"),
                    date_and_time=s["ai_filters"].get("date_and_time"),
                    sentiment=SentimentType(s["ai_filters"]["sentiment"]),
                ),
            )
            for s in data.get("sentences", [])
        ]

        # Parse participants
        participants = [
            {"name": p.get("name"), "email": p.get("email")}
            for p in data.get("meeting_attendees", [])
        ]

        # Build payload
        payload = FirefliesTranscriptReadyPayload(
            id=data["id"],
            title=data["title"],
            date=datetime.fromtimestamp(data["date"] / 1000),
            duration=data["duration"],
            transcript_url=data["transcript_url"],
            audio_url=data.get("audio_url"),
            video_url=data.get("video_url"),
            sentences=sentences,
            summary=data.get("summary", {}).get("overview") if data.get("summary") else None,
            participants=participants,
            speakers=data.get("speakers", []),
            user={
                "user_id": data["user"]["user_id"],
                "email": data["user"]["email"],
                "name": data["user"]["name"],
                "num_transcripts": data["user"]["num_transcripts"],
                "minutes_consumed": data["user"]["minutes_consumed"],
                "is_admin": data["user"]["is_admin"],
            },
            host_email=data["host_email"],
            organizer_email=data["organizer_email"],
            privacy=data["privacy"],
            meeting_link=data.get("meeting_link"),
            calendar_id=data.get("calendar_id"),
            calendar_type=data.get("calendar_type"),
            raw_meeting_info=data.get("meeting_info"),
        )

        source = Source(host="fireflies.ai", type=TriggerType.HOOK, app="fireflies")

        # Look up original upload event via Redis
        # In practice, you'd search by transcript_id or use a mapping
        # For now, we'll just create a new event ID
        event_id = publisher.generate_event_id(
            "fireflies.transcript.ready", transcript_id=payload.id
        )

        # TODO: Look up upload event_id from Redis to set parent_event_ids
        parent_event_ids = []  # Would be [upload_event_id] if we tracked it

        envelope = create_envelope(
            event_type="fireflies.transcript.ready",
            payload=payload,
            source=source,
            correlation_ids=parent_event_ids,
            event_id=event_id,
        )

        await publisher.publish(
            routing_key="fireflies.transcript.ready",
            body=envelope.model_dump(mode="json"),
            event_id=event_id,
            parent_event_ids=parent_event_ids,
        )

        return JSONResponse(
            {"status": "ok", "event_id": str(envelope.event_id), "transcript_id": payload.id}
        )

    except Exception as e:
        # Publish error event
        error_payload = FirefliesTranscriptFailedPayload(
            failed_stage="transcription",
            error_message=str(e),
            transcript_id=body.get("data", {}).get("id") if isinstance(body, dict) else None,
            timestamp=datetime.now(timezone.utc),
        )

        error_envelope = create_envelope(
            event_type="fireflies.transcript.failed",
            payload=error_payload,
            source=Source(host="fireflies.ai", type=TriggerType.HOOK, app="fireflies"),
        )

        await publisher.publish(
            routing_key="fireflies.transcript.failed", body=error_envelope.model_dump(mode="json")
        )

        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Debug Endpoints
# ============================================================================


@app.get("/debug/correlation/{event_id}")
async def debug_correlation(event_id: str):
    """
    Debug endpoint to view correlation chain for an event.

    Returns parents, children, ancestors, and descendants.
    """
    try:
        event_uuid = UUID(event_id)
        debug_data = publisher.debug_correlation(event_uuid)
        return JSONResponse(debug_data)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID format")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug/correlation/{event_id}/chain")
async def get_correlation_chain(event_id: str, direction: str = "ancestors"):
    """
    Get full correlation chain for an event.

    Args:
        event_id: Event UUID
        direction: "ancestors" or "descendants"
    """
    if direction not in ["ancestors", "descendants"]:
        raise HTTPException(
            status_code=400, detail="direction must be 'ancestors' or 'descendants'"
        )

    try:
        event_uuid = UUID(event_id)
        chain = publisher.get_correlation_chain(event_uuid, direction)
        return JSONResponse(
            {"event_id": event_id, "direction": direction, "chain": [str(e) for e in chain]}
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID format")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
