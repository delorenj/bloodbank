"""
OpenClaw hook dispatcher.

Translates commands into OpenClaw /hooks/agent POST calls and maps
responses back to success/failure outcomes.

POST /hooks/agent
Headers:
  Authorization: Bearer {token}
  Content-Type: application/json
  X-Request-Session-Key: agent:{agent_name}:main
Body:
  { "text": "...", "sessionKey": "agent:{agent_name}:main" }
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .dispatcher import Dispatcher, DispatchResult

logger = logging.getLogger(__name__)


@dataclass
class HookResult:
    """Result of an OpenClaw hook dispatch (legacy, use DispatchResult)."""

    success: bool
    status_code: int
    response_body: dict[str, Any] | None = None
    error: str | None = None


class OpenClawHookDispatcher(Dispatcher):
    """Dispatches commands to OpenClaw agents via the hooks API."""

    def __init__(
        self,
        hook_url: str,
        hook_token: str,
        timeout_seconds: float = 30.0,
    ):
        self.hook_url = hook_url
        self.hook_token = hook_token
        self.timeout = timeout_seconds

    @property
    def name(self) -> str:
        return f"openclaw:{self.hook_url[:50]}"

    async def dispatch(
        self,
        *,
        target_agent: str,
        action: str,
        command_id: str,
        issued_by: str,
        priority: str = "normal",
        command_payload: dict[str, Any] | None = None,
    ) -> DispatchResult:
        """
        Send a command to an OpenClaw agent session via hooks API.

        The hook text is structured so the agent can parse the command:
          [Command] action={action} id={command_id} from={issued_by} priority={priority}
          {command_payload as JSON if present}
        """
        session_key = f"agent:{target_agent}:main"

        # Build the hook text
        lines = [
            f"[Command] action={action} id={command_id} from={issued_by} priority={priority}",
        ]
        if command_payload:
            import orjson

            lines.append(
                orjson.dumps(command_payload, option=orjson.OPT_INDENT_2).decode()
            )

        text = "\n".join(lines)

        headers = {
            "Content-Type": "application/json",
        }
        if self.hook_token:
            headers["Authorization"] = f"Bearer {self.hook_token}"

        body = {
            "text": text,
            "message": text,  # OpenClaw expects "message" field
            "sessionKey": session_key,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self.hook_url,
                    headers=headers,
                    json=body,
                )

            if resp.status_code in (200, 202):
                logger.info(
                    f"Hook dispatched: agent={target_agent} action={action} "
                    f"command_id={command_id} status={resp.status_code}"
                )
                try:
                    resp_body = resp.json()
                except Exception:
                    resp_body = None
                return DispatchResult(
                    success=True,
                    status_code=resp.status_code,
                    response_body=resp_body,
                    backend=self.name,
                )
            else:
                error_text = resp.text[:500]
                logger.error(
                    f"Hook failed: agent={target_agent} action={action} "
                    f"status={resp.status_code} body={error_text}"
                )
                return DispatchResult(
                    success=False,
                    status_code=resp.status_code,
                    error=f"HTTP {resp.status_code}: {error_text}",
                    backend=self.name,
                )

        except httpx.TimeoutException:
            logger.error(
                f"Hook timeout: agent={target_agent} action={action} timeout={self.timeout}s"
            )
            return DispatchResult(
                success=False,
                status_code=0,
                error=f"Timeout after {self.timeout}s",
                backend=self.name,
            )
        except httpx.ConnectError as e:
            logger.error(
                f"Hook connection failed: agent={target_agent} action={action} error={e}"
            )
            return DispatchResult(
                success=False,
                status_code=0,
                error=f"Connection failed: {e}",
                backend=self.name,
            )
        except Exception as e:
            logger.error(
                f"Hook dispatch error: agent={target_agent} action={action} error={e}"
            )
            return DispatchResult(
                success=False,
                status_code=0,
                error=str(e),
                backend=self.name,
            )
