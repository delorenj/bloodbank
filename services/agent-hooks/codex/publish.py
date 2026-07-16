#!/usr/bin/env python3
"""Codex CLI → Bloodbank hook publisher (v1 contract).

Thin wrapper around the canonical publisher (``publish.py`` + ``core.publisher``).
Preserves the original CLI semantics: ``cat | publish.py <hookName> [payload-json|end-reason]``.

The embedded ``_DEFAULT_MAP`` is kept for sync.py's fallback drift check.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from clients.codex import CodexAdapter  # noqa: E402
from core.event_map import resolve_map  # noqa: E402
from core.publisher import run  # noqa: E402

_DEFAULT_MAP = CodexAdapter.default_map
HOOK_MAP = resolve_map(CodexAdapter().agent_dir, _DEFAULT_MAP)
CODEX_SOURCE = CodexAdapter.source
CODEX_PRODUCER = CodexAdapter.producer
CODEX_SERVICE = CodexAdapter.service
CODEX_ACTOR = dict(CodexAdapter.actor_base)


def main(argv: list[str]) -> int:
    return run(CodexAdapter(), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
