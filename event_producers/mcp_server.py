from typing import Optional, List, Dict, Any
from mcp.server.fastmcp import FastMCP
from .events import LLMPrompt, LLMResponse, Artifact, create_envelope, create_source
from rabbit import Publisher

mcp = FastMCP("bloodbank")

# GREENFIELD DEPLOYMENT: Correlation tracking enabled by default
# Consistent with http.py - enables correlation tracking for debugging capabilities
publisher = Publisher(enable_correlation_tracking=True)


@mcp.tool()
async def publish_llm_prompt(
    provider: str,
    prompt: str,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    tools: Optional[List[str]] = None,
    project: Optional[str] = None,
    working_dir: Optional[str] = None,
    domain: Optional[str] = None,
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Publish an llm.prompt event to the bus."""
    await publisher.start()
    ev = LLMPrompt(
        provider=provider,
        model=model,
        prompt=prompt,
        temperature=temperature,
        tools=tools or [],
        metadata=metadata or {},
    )
    source = create_source(
        host="bloodbank",
        trigger_type="manual",
        app="mcp"
    )
    env = create_envelope(
        "llm.prompt",
        ev,
        source=source
    )
    await publisher.publish("llm.prompt", env.model_dump(), message_id=env.event_id)
    return {"event_id": env.event_id}


@mcp.tool()
async def publish_llm_response(
    provider: str,
    prompt_id: str,
    response: str,
    model: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Publish an llm.response event, correlated to a prior prompt."""
    await publisher.start()
    ev = LLMResponse(
        provider=provider,
        prompt_id=prompt_id,
        response=response,
        model=model,
        usage=usage,
        metadata=metadata or {},
    )
    source = create_source(
        host="bloodbank",
        trigger_type="manual",
        app="mcp"
    )
    env = create_envelope(
        "llm.response", 
        ev, 
        source=source,
        correlation_ids=[prompt_id] if prompt_id else None
    )
    await publisher.publish(
        "llm.response",
        env.model_dump(),
        message_id=env.event_id,
        correlation_id=str(env.correlation_ids[0]) if env.correlation_ids else None,
    )
    return {"event_id": env.event_id}


@mcp.tool()
async def publish_artifact(
    action: str,
    kind: str,
    uri: str,
    title: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Publish an artifact.{created|updated} event."""
    await publisher.start()
    ev = Artifact(
        action=action, kind=kind, uri=uri, title=title, metadata=metadata or {}
    )
    source = create_source(host="bloodbank", trigger_type="manual", app="mcp")
    env = create_envelope(f"artifact.{action}", ev, source)
    await publisher.publish(f"artifact.{action}", env.model_dump(), message_id=env.event_id)
    return {"event_id": env.event_id}


def run_stdio():
    # Typical entry for stdio transport (spawned by MCP client)
    mcp.run()
