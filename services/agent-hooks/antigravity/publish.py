#!/usr/bin/env python3
"""Antigravity CLI → Bloodbank hook publisher (v1 contract).

Thin wrapper around the canonical publisher (``publish.py`` + ``core.publisher``).
Antigravity hooks invoke the canonical entrypoint as
``cat | python3 ~/.agents/hooks/bloodbank/publish.py --client antigravity --hook <Event>``;
this wrapper preserves direct invocation and exports the identity/map constants
for sync.py's fallback drift check and the health/smoketest tooling.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from clients.antigravity import AntigravityAdapter  # noqa: E402
from core.event_map import resolve_map  # noqa: E402
from core.publisher import run  # noqa: E402

_DEFAULT_MAP = AntigravityAdapter.default_map
HOOK_MAP = resolve_map(AntigravityAdapter().agent_dir, _DEFAULT_MAP)
EVENT_MAP = HOOK_MAP
ANTIGRAVITY_SOURCE = AntigravityAdapter.source
ANTIGRAVITY_PRODUCER = AntigravityAdapter.producer
ANTIGRAVITY_SERVICE = AntigravityAdapter.service
ANTIGRAVITY_ACTOR = dict(AntigravityAdapter.actor_base)


def main(argv: list[str]) -> int:
    return run(AntigravityAdapter(), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
