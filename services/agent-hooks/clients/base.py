"""Base adapter interface for client-specific hook behavior.

The shared publisher (``core.publisher``) calls these methods to get
client-specific decisions at each orchestration step.  Subclasses override
what differs; defaults mirror the most common pattern across existing
publishers.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from core.session import SessionState, _now_iso


class ClientAdapter:
    """Uniform interface for client-specific hook publisher behavior.

    Subclasses set the class-level attributes and override methods that differ
    from the defaults.  The shared publisher calls these in a fixed order;
    see ``core.publisher.run``.
    """

    name: str = ""
    source: str = ""
    producer: str = ""
    service: str = ""
    actor_base: dict[str, Any] = {}
    nats_client_name: str = "bloodbank-agent-hook"

    session_file: Path = Path("/dev/null")
    sessions_dir: Path | None = None
    error_log: Path | None = None

    default_map: dict[str, tuple[str, str]] = {}

    @property
    def agent_dir(self) -> Path:
        """Directory containing the client's event_map.generated.json."""
        raise NotImplementedError

    def read_payload(self, argv: list[str]) -> Any:
        """Read the hook payload (stdin by default; subclasses may extend)."""
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        try:
            out = json.loads(raw)
            return out if isinstance(out, dict) else {"raw": out}
        except json.JSONDecodeError:
            return {"raw": raw}

    def resolve_hook_name(self, argv: list[str], payload: Any) -> str | None:
        """Return the hook/event name from argv or payload, or None."""
        if len(argv) > 1 and argv[1].strip():
            return argv[1].strip()
        return None

    def should_reset_session(self, ce_type: str, hook_name: str) -> bool:
        """Whether to force a fresh session id for this event."""
        return ce_type == "bloodbank.v1.agent.session.started"

    def get_correlation_id(self, session: SessionState, payload: Any) -> str:
        """Correlation ID for the envelope (defaults to session.session_id)."""
        return session.session_id

    def get_causation_id(
        self,
        session: SessionState,
        ce_type: str,
        hook_name: str,
        correlation_id: str,
    ) -> str:
        """Causation ID for the envelope (defaults to session.last_event_id)."""
        if ce_type == "bloodbank.v1.agent.session.started":
            return correlation_id
        return session.last_event_id or correlation_id

    def get_event_id(
        self, session: SessionState, ce_type: str, correlation_id: str
    ) -> str | None:
        """Event ID for the envelope (None = auto-generate UUID).

        Session-start events self-root by default (id == correlation_id).
        """
        if ce_type == "bloodbank.v1.agent.session.started":
            return correlation_id
        return None

    def get_actor(self, payload: Any) -> dict[str, Any]:
        """Return the actor dict for the envelope (may be payload-aware)."""
        return dict(self.actor_base)

    def shape_data(
        self,
        session: SessionState,
        ce_type: str,
        hook_name: str,
        payload: Any,
        argv: list[str],
    ) -> dict[str, Any]:
        """Project the raw hook payload into the v1 schema's data shape."""
        return {"hook": hook_name, "payload": payload}

    def before_publish(
        self, session: SessionState, ce_type: str, payload: Any, argv: list[str]
    ) -> None:
        """Local state updates that must happen once an envelope was built."""

    def post_publish(
        self, session: SessionState, ce_type: str, payload: Any, argv: list[str]
    ) -> None:
        """Side effects after a successful publish (bump tool, archive, etc.)."""
        if ce_type == "bloodbank.v1.agent.session.ended" and self.sessions_dir:
            session.archive(self.sessions_dir)

    def after_publish_attempt(
        self,
        session: SessionState,
        ce_type: str,
        payload: Any,
        argv: list[str],
        *,
        published: bool,
    ) -> None:
        """Side effects that should run even when hooks fail open."""

    def log(self, msg: str) -> None:
        """Log an error/debug message (fail-open; never raises)."""
        if self.error_log:
            try:
                self.error_log.parent.mkdir(parents=True, exist_ok=True)
                if self.error_log.exists() and self.error_log.stat().st_size > 1_048_576:
                    try:
                        self.error_log.rename(
                            self.error_log.with_suffix(self.error_log.suffix + ".1")
                        )
                    except OSError:
                        pass
                with self.error_log.open("a") as f:
                    f.write(f"{_now_iso()} [{os.getpid()}] {msg}\n")
            except OSError:
                pass
        if os.environ.get("BLOODBANK_DEBUG") == "true" or os.environ.get(
            "BLOODBANK_HOOK_VERBOSE"
        ):
            print(f"[bloodbank-{self.name}-hook] {msg}", file=sys.stderr)
