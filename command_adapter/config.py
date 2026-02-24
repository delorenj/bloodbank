"""
Configuration for the command adapter service.

All settings from environment variables — NO hardcoded secrets.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AdapterConfig:
    """Command adapter configuration from environment."""

    # RabbitMQ
    rabbitmq_url: str = field(
        default_factory=lambda: os.environ.get(
            "RABBITMQ_URL",
            os.environ.get("RABBIT_URL", "amqp://delorenj:MISSING_PASSWORD@rabbitmq:5672/"),
        )
    )
    exchange_name: str = field(
        default_factory=lambda: os.environ.get("EXCHANGE_NAME", "bloodbank.events.v1")
    )

    # Redis (for FSM + idempotency)
    redis_url: str = field(
        default_factory=lambda: os.environ.get("REDIS_URL", "redis://33god-redis:6379")
    )

    # OpenClaw hooks
    openclaw_hook_url: str = field(
        default_factory=lambda: os.environ.get(
            "OPENCLAW_HOOK_URL", "http://127.0.0.1:18789/hooks/agent"
        )
    )
    openclaw_hook_token: str = field(
        default_factory=lambda: os.environ.get("OPENCLAW_HOOK_TOKEN", "")
    )

    # Agent roster — which agents this adapter instance handles.
    # Comma-separated list, or "*" for all agents.
    agent_roster: str = field(
        default_factory=lambda: os.environ.get(
            "AGENT_ROSTER",
            "cack,grolf,rererere,lenoon,tonny,tongy,rar,pepe,lalathing,momothecat,yi",
        )
    )

    # Timeouts
    hook_timeout_seconds: float = field(
        default_factory=lambda: float(os.environ.get("HOOK_TIMEOUT_SECONDS", "30"))
    )

    @property
    def agents(self) -> list[str]:
        """Parse agent roster into list."""
        if self.agent_roster.strip() == "*":
            return []  # empty = subscribe to command.#
        return [a.strip() for a in self.agent_roster.split(",") if a.strip()]
