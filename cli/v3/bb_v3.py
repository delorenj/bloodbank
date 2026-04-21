#!/usr/bin/env python3
"""Bloodbank v3 operator CLI (``bb_v3``) — stub skeleton.

This module is the first-wave scaffold for the 33GOD v3 operator CLI. It
provides argparse-based subcommands (``doctor``, ``trace``, ``replay``,
``emit``) that are intentionally side-effect-free in this wave:

* No network I/O (no HTTP, no NATS, no Dapr sidecar calls).
* No third-party Python dependencies -- standard library only.
* No publishing of events or commands.

Architectural context:

* ``docs/architecture/v3-implementation-plan.md`` in the 33GOD metarepo is
  the source of truth for the v3 platform implementation plan.
* ``docs/architecture/ADR-0001-v3-platform-pivot.md`` ratifies the
  non-negotiable architecture decisions (Dapr + NATS JetStream +
  CloudEvents + AsyncAPI + EventCatalog + Apicurio Registry).

Per ADR-0001, this CLI is an **operator tool**, not the primary production
publish path. Production traffic flows through Dapr publishers embedded in
services using Holyfields-generated SDKs.

Design notes:

* ``doctor`` resolves the bloodbank root from this file's own location
  (``__file__``), so it works regardless of the current working directory.
* ``ops/v3/bootstrap/check-platform.sh`` may not yet exist when ``doctor``
  is run during the scaffold bring-up (it is produced by BB-22 / V3-006
  alongside this ticket). To keep ticket-by-ticket ordering within Group B
  flexible, ``doctor`` treats that single file as a ``WARN`` when missing
  rather than ``FAIL``. V3-011 (final verification) tightens this to a hard
  failure once the full scaffold wave is required to be present at once.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Scaffold manifest
# ---------------------------------------------------------------------------
#
# Each entry is (path-relative-to-bloodbank-root, severity-when-missing).
# Severity is either "FAIL" (blocks exit 0) or "WARN" (reported but not
# blocking). See the module docstring for why one file is a WARN during
# this wave.
SCAFFOLD_MANIFEST: tuple[tuple[str, str], ...] = (
    ("compose/v3/docker-compose.yml", "FAIL"),
    ("compose/v3/components/pubsub.yaml", "FAIL"),
    ("compose/v3/components/statestore.yaml", "FAIL"),
    ("compose/v3/components/secretstore.yaml", "FAIL"),
    ("compose/v3/nats/streams.json", "FAIL"),
    ("compose/v3/README.md", "FAIL"),
    # BB-22 / V3-006 companion file. Treated as WARN here until V3-011
    # tightens this to FAIL across the whole scaffold wave.
    ("ops/v3/bootstrap/check-platform.sh", "WARN"),
    # Self-check: this file.
    ("cli/v3/bb_v3.py", "FAIL"),
)


def bloodbank_root() -> Path:
    """Return the bloodbank repo root based on this file's location.

    This file lives at ``<bloodbank>/cli/v3/bb_v3.py``, so the root is two
    parents up. Resolving from ``__file__`` (rather than ``os.getcwd()``)
    means ``doctor`` works from any current working directory.
    """
    return Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def cmd_doctor(_args: argparse.Namespace) -> int:
    """Static, local-only check that the v3 scaffold files are present.

    Prints one line per checked artifact:

    * ``PASS <path>``               -- file exists.
    * ``WARN <path>``               -- file missing but non-blocking for this wave.
    * ``FAIL <path>: <reason>``     -- file missing and blocking.

    Exits 0 iff there are no ``FAIL`` lines.
    """
    root = bloodbank_root()
    fail_count = 0
    pass_count = 0
    warn_count = 0

    for rel_path, severity in SCAFFOLD_MANIFEST:
        target = root / rel_path
        if target.is_file():
            print(f"PASS {rel_path}")
            pass_count += 1
            continue

        # Missing. Decide severity.
        reason = "missing or not a regular file"
        if severity == "WARN":
            print(f"WARN {rel_path}")
            warn_count += 1
        else:
            print(f"FAIL {rel_path}: {reason}")
            fail_count += 1

    total = len(SCAFFOLD_MANIFEST)
    print(
        f"doctor: {pass_count}/{total} artifacts present "
        f"({warn_count} warn, {fail_count} fail)"
    )
    return 0 if fail_count == 0 else 1


def cmd_trace(_args: argparse.Namespace) -> int:
    """Stub: event-chain trace walker.

    Production-capable tracing will consult NATS JetStream and a schema
    registry. That is deferred to a later ticket; see ``ops/v3/trace/README.md``.
    """
    print("trace: not yet implemented -- see ops/v3/trace/README.md")
    return 0


def cmd_replay(_args: argparse.Namespace) -> int:
    """Stub: replay a historical event into the sandbox.

    Production-capable replay preserves original IDs and adds replay
    metadata; see ``ops/v3/replay/README.md`` (V3-007).
    """
    print("replay: not yet implemented -- see ops/v3/replay/README.md")
    return 0


def cmd_emit(_args: argparse.Namespace) -> int:
    """Stub: emit a handcrafted event for smoke-testing.

    Operator emission requires a Dapr sidecar (per ADR-0001), which is not
    wired in this wave. Future tickets will add this behind a safety flag.
    """
    print(
        "emit: not yet implemented -- operator emission requires Dapr "
        "sidecar; will land in a later ticket"
    )
    return 0


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bb_v3",
        description=(
            "Bloodbank v3 operator CLI (stub). Safe for local use: no "
            "network I/O, no third-party dependencies, no published traffic."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_doctor = subparsers.add_parser(
        "doctor",
        help="static local scaffold check (no network, no Docker)",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_trace = subparsers.add_parser(
        "trace",
        help="walk an event chain by correlation/causation IDs (stub)",
    )
    p_trace.set_defaults(func=cmd_trace)

    p_replay = subparsers.add_parser(
        "replay",
        help="replay a historical event (stub)",
    )
    p_replay.set_defaults(func=cmd_replay)

    p_emit = subparsers.add_parser(
        "emit",
        help="emit a handcrafted event (stub)",
    )
    p_emit.set_defaults(func=cmd_emit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # argparse with required=True guarantees args.func is set.
    return int(args.func(args))


if __name__ == "__main__":
    # Defensive: ensure we pass through to our own entrypoint, never
    # anything external. ``os.environ`` is read-only here, used only so
    # this line has some purpose and to document that we take no env-driven
    # side effects in this wave.
    _ = os.environ.get("BLOODBANK_V3_NOOP", "")
    sys.exit(main(sys.argv[1:]))
