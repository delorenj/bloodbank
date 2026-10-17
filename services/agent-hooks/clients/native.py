"""Payload-compatible adapters with independent CLI identity and state.

Gemini and Kimi use the same snake_case hook data fields as Codex/Claude.
The OpenCode plugin normalizes its SDK payload to these fields before sending
it to the hub. Reusing shaping must never reuse another CLI's actor or map.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from core.session import SessionState

from .base import reply_fields
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

    def shape_data(self, session: SessionState, ce_type: str, hook_name: str,
                   payload: Any, argv: list[str]) -> dict[str, Any]:
        data = super().shape_data(session, ce_type, hook_name, payload, argv)
        if ce_type == "bloodbank.conversation.turn.completed" and "reply_text" not in data:
            # session.idle carries no text, but opencode persists every
            # message; read the turn's final assistant reply from its store.
            sid = payload.get("session_id") if isinstance(payload, dict) else None
            data.update(reply_fields(opencode_last_reply(sid)))
        return data


def opencode_db_path() -> Path:
    override = os.environ.get("BB_OPENCODE_DB")
    return Path(override) if override else Path.home() / ".local/share/opencode/opencode.db"


def opencode_last_reply(session_id: Any, *, db: Path | None = None) -> str | None:
    """Text of the session's final assistant message, from opencode's own DB.

    Read-only, bounded to the newest few rows, 0.5 s busy timeout, fail-open.
    Walks back from the newest row: skips idle markers, takes the first
    assistant row that has text content, stops at the user prompt that opened
    the turn (so a turn with no reply never inherits the previous turn's).
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    path = db or opencode_db_path()
    try:
        if not path.is_file():
            return None
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        try:
            rows = con.execute(
                "SELECT type, data FROM session_message WHERE session_id = ? "
                "ORDER BY seq DESC LIMIT 40", (session_id,)).fetchall()
        finally:
            con.close()
        for kind, raw in rows:
            if kind == "user":
                return None
            if kind != "assistant":
                continue
            try:
                content = json.loads(raw).get("content")
            except (ValueError, AttributeError):
                continue
            if not isinstance(content, list):
                continue
            texts = [b["text"] for b in content
                     if isinstance(b, dict) and b.get("type") == "text"
                     and isinstance(b.get("text"), str) and b["text"].strip()]
            if texts:
                return "\n\n".join(texts)
    except Exception:
        return None
    return None
