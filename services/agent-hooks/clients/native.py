"""Payload-compatible adapters with independent CLI identity and state.

Gemini and Kimi use the same snake_case hook data fields as Codex/Claude.
The OpenCode plugin normalizes its SDK payload to these fields before sending
it to the hub. Reusing shaping must never reuse another CLI's actor or map.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .codex import CodexAdapter


class NativeAdapter(CodexAdapter):
    default_map = {}
    sessions_dir = None

    def __init__(self) -> None:
        self.source = f"urn:33god:agent:{self.name}"
        self.producer = self.name
        self.service = f"{self.name}-hooks"
        self.nats_client_name = f"agent-hooks-{self.name}"
        state = Path.home() / ".local/state/33god/agent-hooks" / self.name
        self.session_file = state / "legacy-session.json"
        self.error_log = state / "publish-errors.log"

    @property
    def agent_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / self.name

    def get_actor(self, payload: Any) -> dict[str, Any]:
        actor = super().get_actor(payload)
        if isinstance(payload, dict):
            model = payload.get("model")
            if isinstance(model, dict):
                actor["model"] = model.get("modelID") or model.get("id")
                actor["provider"] = model.get("providerID") or actor["provider"]
            if isinstance(payload.get("provider"), str):
                actor["provider"] = payload["provider"]
        return actor


class GeminiAdapter(NativeAdapter):
    name = "gemini"
    actor_base = {"type": "agent_cli", "agent_id": "bloodbank.agent.gemini",
                  "cli": "gemini", "provider": "google", "model": None}


class KimiAdapter(NativeAdapter):
    name = "kimi"
    actor_base = {"type": "agent_cli", "agent_id": "bloodbank.agent.kimi",
                  "cli": "kimi", "provider": "moonshot", "model": None}


class OpenCodeAdapter(NativeAdapter):
    name = "opencode"
    actor_base = {"type": "agent_cli", "agent_id": "bloodbank.agent.opencode",
                  "cli": "opencode", "provider": None, "model": None}
