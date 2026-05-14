#!/usr/bin/env python3
"""Bloodbank operator CLI (``bb``) — scaffold + operator snapshots.

This module is the first-wave scaffold for the 33GOD operator CLI. It
provides argparse-based subcommands (``doctor``, ``trace``, ``replay``,
``emit``, ``repo-health``) that are intentionally low-risk in this wave:

* No third-party Python dependencies -- standard library only.
* No publishing of events or commands.

Notes:

* ``repo-health`` is read-only and may call ``gh``/``git`` to gather
  evidence snapshots (issues/PRs/checks + working-tree status).
* ``emit`` remains guarded and does not publish in this wave.

Architectural context:

* ADR-0001 in the 33GOD metarepo (TBD) will ratify the non-negotiable
  architecture decisions (Dapr + NATS JetStream + CloudEvents + AsyncAPI
  + EventCatalog + Apicurio Registry).

Per ADR-0001, this CLI is an **operator tool**, not the primary production
publish path. Production traffic flows through Dapr publishers embedded in
services using Holyfields-generated SDKs.

Design notes:

* ``doctor`` resolves the bloodbank root from this file's own location
  (``__file__``), so it works regardless of the current working directory.
* Every entry in ``SCAFFOLD_MANIFEST`` is ``FAIL`` on missing. The WARN
  severity remains supported as a general mechanism but no entry currently
  uses it. An earlier iteration kept ``ops/bootstrap/check-platform.sh``
  as ``WARN`` during Group B bring-up; that file now exists and is
  required, so the entry was upgraded to ``FAIL`` as part of the scaffold
  hardening wave (2026-04-22).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
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
    ("compose/docker-compose.yml", "FAIL"),
    ("compose/components/pubsub.yaml", "FAIL"),
    ("compose/components/statestore.yaml", "FAIL"),
    ("compose/components/secretstore.yaml", "FAIL"),
    ("compose/nats/streams.json", "FAIL"),
    ("compose/nats/init.sh", "FAIL"),
    ("compose/README.md", "FAIL"),
    ("ops/bootstrap/check-platform.sh", "FAIL"),
    # Self-check: this file.
    ("cli/bb.py", "FAIL"),
)


def bloodbank_root() -> Path:
    """Return the bloodbank repo root based on this file's location.

    This file lives at ``<bloodbank>/cli/bb.py``, so the root is two
    parents up. Resolving from ``__file__`` (rather than ``os.getcwd()``)
    means ``doctor`` works from any current working directory.
    """
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def cmd_doctor(_args: argparse.Namespace) -> int:
    """Static, local-only check that the scaffold files are present.

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
    registry. That is deferred to a later ticket; see ``ops/trace/README.md``.
    """
    print("trace: not yet implemented -- see ops/trace/README.md")
    return 0


def cmd_replay(_args: argparse.Namespace) -> int:
    """Stub: replay a historical event into the sandbox.

    Production-capable replay preserves original IDs and adds replay
    metadata; see ``ops/replay/README.md`` (V3-007).
    """
    print("replay: not yet implemented -- see ops/replay/README.md")
    return 0


def cmd_emit(_args: argparse.Namespace) -> int:
    """Stub: emit a handcrafted event for smoke-testing.

    Operator emission is only valid through a Dapr sidecar per ADR-0001.
    The real implementation will require ``DAPR_HTTP_PORT`` to be set in
    the environment and will refuse to publish via any other transport.
    The check below documents and enforces that contract at the CLI
    boundary: even the stub refuses to pretend it published when no Dapr
    sidecar is advertised.
    """
    dapr_http_port = os.environ.get("DAPR_HTTP_PORT", "")
    if not dapr_http_port:
        print(
            "emit: refusing -- DAPR_HTTP_PORT is unset. Operator emission "
            "requires a Dapr sidecar; run this under `dapr run` or set "
            "DAPR_HTTP_PORT explicitly. See ADR-0001 Role reassignments."
        )
        return 2

    # Guardrail passed; implementation still pending.
    print(
        "emit: not yet implemented -- DAPR_HTTP_PORT is set "
        f"(={dapr_http_port}); awaiting Holyfields SDK for envelope "
        "construction (HOLYF-2)."
    )
    return 0


def _run(root: Path, *argv: str) -> tuple[int, str, str]:
    """Run a command from ``root`` and return ``(rc, stdout, stderr)``."""
    proc = subprocess.run(
        argv,
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return int(proc.returncode), proc.stdout.strip(), proc.stderr.strip()


def cmd_repo_health(args: argparse.Namespace) -> int:
    """Print a concise repo/ticket/PR/check snapshot for BMAD evidence."""
    root = bloodbank_root()

    rc_git, git_out, git_err = _run(root, "git", "status", "--short", "--branch")
    if rc_git != 0:
        print(f"git_status: ERROR ({git_err or 'unknown git error'})")
        return 2

    git_line = git_out.splitlines()[0] if git_out else "<no status output>"
    print(f"git_status: {git_line}")

    fail_count = 0

    rc_issue, issue_out, issue_err = _run(
        root,
        "gh",
        "issue",
        "list",
        "--state",
        "open",
        "--limit",
        str(args.limit),
        "--json",
        "number,title,url",
    )
    issues: list[dict[str, object]] = []
    if rc_issue == 0 and issue_out:
        try:
            issues = json.loads(issue_out)
        except json.JSONDecodeError:
            fail_count += 1
            print("issues_open: ERROR (invalid JSON from gh issue list)")
    elif rc_issue != 0:
        fail_count += 1
        print(f"issues_open: ERROR ({issue_err or 'gh issue list failed'})")

    if rc_issue == 0:
        print(f"issues_open: {len(issues)}")
        for it in issues:
            print(f"- issue #{it['number']}: {it['title']} ({it['url']})")

    rc_pr, pr_out, pr_err = _run(
        root,
        "gh",
        "pr",
        "list",
        "--state",
        "open",
        "--limit",
        str(args.limit),
        "--json",
        "number,title,url,updatedAt",
    )
    prs: list[dict[str, object]] = []
    if rc_pr == 0 and pr_out:
        try:
            prs = json.loads(pr_out)
        except json.JSONDecodeError:
            fail_count += 1
            print("prs_open: ERROR (invalid JSON from gh pr list)")
    elif rc_pr != 0:
        fail_count += 1
        print(f"prs_open: ERROR ({pr_err or 'gh pr list failed'})")

    if rc_pr == 0:
        print(f"prs_open: {len(prs)}")
        for pr in prs:
            print(f"- pr #{pr['number']}: {pr['title']} ({pr['url']})")

    if prs:
        newest = sorted(prs, key=lambda x: str(x.get("updatedAt", "")), reverse=True)[0]
        pr_number = int(newest["number"])
        print(f"latest_pr_checks: #{pr_number}")
        rc_checks, checks_out, checks_err = _run(root, "gh", "pr", "checks", str(pr_number))
        if rc_checks in (0, 8) and checks_out:
            for line in checks_out.splitlines():
                print(f"  {line}")
        else:
            fail_count += 1
            print(f"  ERROR ({checks_err or 'gh pr checks failed'})")
    else:
        print("latest_pr_checks: none (no open PRs)")

    return 0 if fail_count == 0 else 1


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bb",
        description=(
            "Bloodbank operator CLI (scaffold). Default commands are "
            "side-effect free; repo-health is read-only evidence capture."
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

    p_repo_health = subparsers.add_parser(
        "repo-health",
        help="print git/issue/PR/check snapshot for BMAD evidence",
    )
    p_repo_health.add_argument(
        "--limit",
        type=int,
        default=5,
        help="max open issues/PRs to list (default: 5)",
    )
    p_repo_health.set_defaults(func=cmd_repo_health)

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
    _ = os.environ.get("BLOODBANK_NOOP", "")
    sys.exit(main(sys.argv[1:]))
