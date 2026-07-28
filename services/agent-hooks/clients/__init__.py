"""Client adapters for the canonical Bloodbank hook publisher.

Each adapter encapsulates client-specific behavior (payload reading, hook-name
resolution, session paths, data shaping, actor defaults) behind a uniform
interface consumed by ``core.publisher``.  The registry below lets the
canonical entrypoint (``publish.py``) select the right adapter by name.

Stdlib-only.
"""
from __future__ import annotations

from .base import ClientAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .copilot import CopilotAdapter
from .hermes import HermesAdapter

REGISTRY: dict[str, type[ClientAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "copilot": CopilotAdapter,
    "hermes": HermesAdapter,
}


def get_adapter(name: str) -> ClientAdapter:
    """Return an instantiated adapter for *name*, or raise KeyError."""
    cls = REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"unknown client adapter: {name!r} (available: {sorted(REGISTRY)})")
    return cls()
