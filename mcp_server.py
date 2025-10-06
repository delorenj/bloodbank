from typing import Optional, List, Dict, Any
from mcp.server.fastmcp import FastMCP
from .events import LLMPrompt, LLMResponse, Artifact, envelope_for
from .rabbit import Publisher

mcp = FastMCP("bloodbank")
publisher = Publisher()


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
    env = envelope_for(
        "llm.prompt",
        source="mcp/bloodbank",
        data=ev,
        project=project,
        working_dir=working_dir,
        domain=domain,
        tags=tags or [],
    )
    await publisher.publish("llm.prompt", env.model_dump(), message_id=env.id)
    return {"event_id": env.id}


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
    env = envelope_for(
        "llm.response", source="mcp/bloodbank", data=ev, correlation_id=prompt_id
    )
    await publisher.publish(
        "llm.response",
        env.model_dump(),
        message_id=env.id,
        correlation_id=env.correlation_id,
    )
    return {"event_id": env.id}


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
    env = envelope_for(f"artifact.{action}", source="mcp/bloodbank", data=ev)
    await publisher.publish(f"artifact.{action}", env.model_dump(), message_id=env.id)
    return {"event_id": env.id}


def run_stdio():
    # Typical entry for stdio transport (spawned by MCP client)
    mcp.run()
