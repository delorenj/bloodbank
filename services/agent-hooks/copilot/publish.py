#!/usr/bin/env python3
"""GitHub Copilot CLI → Bloodbank hook publisher (v1 contract).

Thin wrapper around the canonical publisher (``publish.py`` + ``core.publisher``).
Preserves the original CLI semantics: ``publish.py <hookName>``.

The embedded ``_DEFAULT_MAP`` is kept for sync.py's fallback drift check.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from clients.copilot import CopilotAdapter  # noqa: E402
from core.event_map import resolve_map  # noqa: E402
from core.publisher import run  # noqa: E402

_DEFAULT_MAP = CopilotAdapter.default_map
HOOK_MAP = resolve_map(CopilotAdapter().agent_dir, _DEFAULT_MAP)
COPILOT_SOURCE = CopilotAdapter.source
COPILOT_PRODUCER = CopilotAdapter.producer
COPILOT_SERVICE = CopilotAdapter.service
COPILOT_ACTOR = dict(CopilotAdapter.actor_base)


def main(argv: list[str]) -> int:
    return run(CopilotAdapter(), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
