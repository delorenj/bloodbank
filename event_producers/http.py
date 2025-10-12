from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from .events import LLMPrompt, LLMResponse, Artifact, envelope_for
from .rabbit import Publisher
from .config import settings

app = FastAPI(title="bloodbank", version="0.1.0")
publisher = Publisher()


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


@app.post("/events/llm/prompt")
async def publish_prompt(ev: LLMPrompt, request: Request):
    env = envelope_for("llm.prompt", source="http/" + request.client.host, data=ev)
    await publisher.publish("llm.prompt", env.model_dump(), message_id=env.id)
    return JSONResponse(env.model_dump())


@app.post("/events/llm/response")
async def publish_response(ev: LLMResponse, request: Request):
    env = envelope_for(
        "llm.response",
        source="http/" + request.client.host,
        data=ev,
        correlation_id=ev.prompt_id,
    )
    await publisher.publish(
        "llm.response",
        env.model_dump(),
        message_id=env.id,
        correlation_id=env.correlation_id,
    )
    return JSONResponse(env.model_dump())


@app.post("/events/artifact")
async def publish_artifact(ev: Artifact, request: Request):
    env = envelope_for(
        f"artifact.{ev.action}", source="http/" + request.client.host, data=ev
    )
    await publisher.publish(
        f"artifact.{ev.action}", env.model_dump(), message_id=env.id
    )
    return JSONResponse(env.model_dump())


# --- example: Fireflies webhook → Artifact.created ---
@app.post("/webhooks/fireflies")
async def fireflies_webhook(req: Request):
    body = await req.json()
    # adapt to your Fireflies payload fields:
    transcript_url = body["data"]["url"]
    title = body["data"]["title"]
    ev = Artifact(
        action="created",
        kind="transcript",
        uri=transcript_url,
        title=title,
        metadata={"source": "fireflies"},
    )
    env = envelope_for("artifact.created", source="http/fireflies", data=ev)
    await publisher.publish("artifact.created", env.model_dump(), message_id=env.id)
    return {"status": "ok", "event_id": env.id}
