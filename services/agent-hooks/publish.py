#!/usr/bin/env python3
"""Canonical Bloodbank agent-hook publisher entrypoint.

Single entrypoint for all agent CLI hooks.  Selects a client adapter and
delegates to the shared orchestration in ``core.publisher``.

Usage:
    python3 publish.py --client <claude|codex|copilot|hermes> --hook <event> [args...]
    python3 publish.py <client> <hook> [args...]            # positional form

The ``--hook`` flag and the first positional form are interchangeable; this
tolerates generated configs that pass the hook name as a bare positional arg.

Fail-open by default; never blocks the agent unless BLOODBANK_HOOK_STRICT=1.
Stdlib-only.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from clients import REGISTRY, get_adapter  # noqa: E402
from core.publisher import run  # noqa: E402


def _parse_argv(argv: list[str]) -> tuple[str, str, list[str]]:
    """Extract (client_name, hook_name, remaining_argv) from argv.

    Supports two forms:
      --client <name> --hook <event> [rest...]
      <name> <event> [rest...]

    The ``--hook`` value is injected into the returned argv at position 1
    so that ``adapter.resolve_hook_name`` and ``adapter.read_payload`` see
    the same shape as the legacy per-client publishers.
    """
    client_name: str | None = None
    hook_name: str | None = None
    rest: list[str] = []

    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--client" and i + 1 < len(argv):
            client_name = argv[i + 1]
            i += 2
        elif arg == "--hook" and i + 1 < len(argv):
            hook_name = argv[i + 1]
            i += 2
        elif arg.startswith("--client="):
            client_name = arg.split("=", 1)[1]
            i += 1
        elif arg.startswith("--hook="):
            hook_name = arg.split("=", 1)[1]
            i += 1
        else:
            rest.append(arg)
            i += 1

    if client_name is None and len(rest) >= 1:
        client_name = rest.pop(0)

    if hook_name is None and len(rest) >= 1:
        hook_name = rest.pop(0)

    if client_name is None:
        client_name = ""

    if hook_name is None:
        hook_name = ""

    # Reconstruct argv so adapters see [prog, hook, ...rest]
    out_argv = [argv[0], hook_name] + rest
    return client_name, hook_name, out_argv


def main(argv: list[str]) -> int:
    client_name, hook_name, run_argv = _parse_argv(argv)

    if not client_name or client_name not in REGISTRY:
        print(
            f"usage: publish.py --client <{'|'.join(sorted(REGISTRY))}> --hook <event> [args...]",
            file=sys.stderr,
        )
        return 2

    if not hook_name:
        print(
            f"usage: publish.py --client {client_name} --hook <event> [args...]",
            file=sys.stderr,
        )
        return 2

    adapter = get_adapter(client_name)
    return run(adapter, run_argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
