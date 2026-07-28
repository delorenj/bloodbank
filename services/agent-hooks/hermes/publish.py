#!/usr/bin/env python3
"""Hermes agent → Bloodbank hook publisher (v1 contract).

Thin wrapper around the canonical publisher (``publish.py`` + ``core.publisher``).
Preserves the original CLI semantics: ``publish.py <hermes-event>``.

The embedded ``_DEFAULT_MAP`` is kept for sync.py's fallback drift check.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from clients.hermes import HermesAdapter  # noqa: E402
from core.event_map import resolve_map  # noqa: E402
from core.publisher import run  # noqa: E402

_DEFAULT_MAP = HermesAdapter.default_map
HOOK_MAP = resolve_map(HermesAdapter().agent_dir, _DEFAULT_MAP)
HERMES_SOURCE = HermesAdapter.source
HERMES_PRODUCER = HermesAdapter.producer
HERMES_SERVICE = HermesAdapter.service
HERMES_ACTOR = dict(HermesAdapter.actor_base)


def main(argv: list[str]) -> int:
    return run(HermesAdapter(), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
