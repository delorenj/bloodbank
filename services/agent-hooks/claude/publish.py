#!/usr/bin/env python3
"""Claude Code → Bloodbank hook publisher (v1 contract).

Thin wrapper around the canonical publisher (``publish.py`` + ``core.publisher``).
Preserves the original CLI semantics: ``publish.py <event-type> [end-reason]``.

The embedded ``_DEFAULT_MAP`` is kept for sync.py's fallback drift check.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from clients.claude import ClaudeAdapter  # noqa: E402
from core.event_map import resolve_map  # noqa: E402
from core.publisher import run  # noqa: E402

_DEFAULT_MAP = ClaudeAdapter.default_map
EVENT_MAP = resolve_map(ClaudeAdapter().agent_dir, _DEFAULT_MAP)
CLAUDE_SOURCE = ClaudeAdapter.source
CLAUDE_PRODUCER = ClaudeAdapter.producer
CLAUDE_SERVICE = ClaudeAdapter.service
CLAUDE_ACTOR = dict(ClaudeAdapter.actor_base)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    return run(ClaudeAdapter(), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
