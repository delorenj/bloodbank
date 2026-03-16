"""
HTTP dispatcher for non-OpenClaw agent backends.

Dispatches commands to arbitrary HTTP endpoints, useful for:
- External agent services
- Webhook-based integrations
- Custom agent backends

Configuration:
    HTTP_DISPATCHER_<AGENT>_URL: Target endpoint for the agent
    HTTP_DISPATCHER_TIMEOUT_SECONDS: Request timeout (default: 30)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .dispatcher import Dispatcher, DispatchResult

logger = logging.getLogger(__name__)


@dataclass
class HTTPDispatcherConfig:
    """Configuration for HTTP dispatcher."""

    endpoint_url: str
    timeout_seconds: float = 30.0
    auth_token: str | None = None
    headers: dict[str, str] | None = None


class HTTPDispatcher(Dispatcher):
    """Dispatches commands to HTTP endpoints.

    Sends POST requests with JSON payload to configured endpoints.
    The payload structure matches the command envelope format.
    """

    def __init__(self, config: HTTPDispatcherConfig):
        self.config = config
        self._name = f"http:{config.endpoint_url[:50]}"

    @property
    def name(self) -> str:
        return self._name

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
        """Send command to HTTP endpoint."""
        import orjson

        headers = {
            "Content-Type": "application/json",
            "X-Command-ID": command_id,
            "X-Agent": target_agent,
            "X-Action": action,
        }

        if self.config.auth_token:
            headers["Authorization"] = f"Bearer {self.config.auth_token}"

        if self.config.headers:
            headers.update(self.config.headers)

        body = {
            "command_id": command_id,
            "target_agent": target_agent,
            "action": action,
            "issued_by": issued_by,
            "priority": priority,
            "payload": command_payload or {},
        }

        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                resp = await client.post(
                    self.config.endpoint_url,
                    headers=headers,
                    json=body,
                )

            if resp.status_code in (200, 201, 202):
                logger.info(
                    f"HTTP dispatch success: agent={target_agent} action={action} "
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
                    f"HTTP dispatch failed: agent={target_agent} action={action} "
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
                f"HTTP dispatch timeout: agent={target_agent} action={action} "
                f"timeout={self.config.timeout_seconds}s"
            )
            return DispatchResult(
                success=False,
                status_code=0,
                error=f"Timeout after {self.config.timeout_seconds}s",
                backend=self.name,
            )
        except httpx.ConnectError as e:
            logger.error(
                f"HTTP dispatch connection failed: agent={target_agent} "
                f"action={action} error={e}"
            )
            return DispatchResult(
                success=False,
                status_code=0,
                error=f"Connection failed: {e}",
                backend=self.name,
            )
        except Exception as e:
            logger.error(
                f"HTTP dispatch error: agent={target_agent} action={action} error={e}"
            )
            return DispatchResult(
                success=False,
                status_code=0,
                error=str(e),
                backend=self.name,
            )


def http_dispatcher_factory(url: str, timeout: float = 30.0) -> HTTPDispatcher:
    """Factory function to create HTTP dispatchers."""
    return HTTPDispatcher(
        HTTPDispatcherConfig(
            endpoint_url=url,
            timeout_seconds=timeout,
        )
    )
