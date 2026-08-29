#!/usr/bin/env python3
"""Bloodbank operator CLI (``bb``) — scaffold + operator snapshots.

This module is the first-wave scaffold for the 33GOD operator CLI. It
provides argparse-based subcommands (``contract``, ``doctor``, ``trace``,
``replay``, ``emit``, ``repo-health``, ``verify-envelope``):

* No third-party Python dependencies -- standard library only.

Notes:

* ``contract`` prints the naming vocabulary -- the legal domains, entities,
  event actions and command actions -- read live from
  ``services/agent-hooks/core/validate.py``, which stays the one source of
  truth because it is where enforcement happens. ``--json`` exists so
  generators and agent skills consume the vocabulary rather than hardcoding
  literals that drift out of it.
* ``repo-health`` is read-only and may call ``gh``/``git`` to gather
  evidence snapshots (issues/PRs/checks + working-tree status).
* ``emit`` is a passthrough to ``bin/bb-emit``; ``bb emit --check`` is the
  dry run to reach for while writing a new producer.

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
import time
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


def cmd_verify_envelope(args: argparse.Namespace) -> int:
    """Validate a Bloodbank v1 envelope against the contract.

    Reads JSON from ``--file`` or stdin, runs ``core.validate.assert_contract``,
    and prints PASS / FAIL. Useful for replay scripts, ntfy debugging, and
    CI gates that want to assert any envelope they touch matches the
    Bloodbank Event Naming Contract v1 (docs/event-naming.md).
    """
    sys.path.insert(0, str(bloodbank_root() / "services" / "agent-hooks"))
    try:
        from core.validate import assert_contract, ContractViolation  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        print(f"verify-envelope: cannot import validator: {exc!r}", file=sys.stderr)
        return 2

    src = args.file
    if src and src != "-":
        try:
            raw = Path(src).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"verify-envelope: cannot read {src}: {exc}", file=sys.stderr)
            return 2
    else:
        raw = sys.stdin.read()

    if not raw.strip():
        print("verify-envelope: empty input", file=sys.stderr)
        return 2

    try:
        env = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"verify-envelope: input is not JSON: {exc}", file=sys.stderr)
        return 2

    try:
        assert_contract(env)
    except ContractViolation as exc:
        print(f"FAIL {env.get('type')!r}: {exc}")
        return 1

    print(
        f"PASS type={env.get('type')!r} "
        f"kind={env.get('kind')!r} "
        f"subject={env.get('subject')!r}"
    )
    return 0


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


def _load_validate():
    """Import core.validate, the single source of truth for the allowlists.

    The vocabulary deliberately does NOT get copied into this file. Enforcement
    lives in ``services/agent-hooks/core/validate.py`` and this CLI is a window
    onto it -- a second copy here would be one more thing to drift, which is the
    exact failure this command exists to end.
    """
    sys.path.insert(0, str(bloodbank_root() / "services" / "agent-hooks"))
    from core import validate  # noqa: WPS433
    return validate


def cmd_contract(args: argparse.Namespace) -> int:
    """Print the naming vocabulary: domains, entities, event + command actions.

    This is the discoverability half of the contract. The allowlists in
    ``core/validate.py`` were reachable only by failing against them: an author
    who guessed a token learned they were wrong when their event silently did
    not arrive. Nine event families were invented and shipped that way. Printing
    the vocabulary -- and, with ``--json``, letting generators and agent skills
    CONSUME it instead of hard-coding literals -- is what makes the contract
    answerable before the first publish rather than after it.
    """
    try:
        validate = _load_validate()
    except Exception as exc:  # noqa: BLE001
        print(f"contract: cannot import validator: {exc!r}", file=sys.stderr)
        return 2

    payload = {
        "grammar": {
            "type": "bloodbank.<domain>.<entity>.<action>",
            "subject": "bloodbank.<kind>.<domain>.<entity>.<action>",
            "kind_markers": dict(validate.KIND_MARKERS),
            "schemaref": "bloodbank.<domain>.<entity>.<action>.v<n>",
            "dataschema": (
                "apicurio://holyfields/bloodbank.<domain>.<entity>.<action>/versions/<n>"
            ),
            "note": (
                "Versioning lives ONLY in schemaref/dataschema. There is no version "
                "token in a type or a subject. Identity (repo, agent) lives in the "
                "payload -- data.* and actor.* -- never in a token."
            ),
        },
        "domains": sorted(validate.ALLOWED_DOMAINS),
        "entities": sorted(validate.ALLOWED_ENTITIES),
        "event_actions": sorted(validate.EVENT_ACTIONS),
        "command_actions": sorted(validate.COMMAND_ACTIONS),
        "banned_tokens": sorted(validate.BANNED_TOKENS),
        "source": "services/agent-hooks/core/validate.py",
        "doc": "docs/event-naming.md",
    }

    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=False))
        return 0

    def _section(title: str, note: str, values: list[str]) -> None:
        print(f"\n{title}  ({len(values)})")
        print(f"  {note}")
        line = "   "
        for value in values:
            if len(line) + len(value) + 1 > 78:
                print(line)
                line = "   "
            line += f" {value}"
        if line.strip():
            print(line)

    g = payload["grammar"]
    print("Bloodbank Event Naming Contract -- the legal vocabulary")
    print(f"  source: {payload['source']}")
    print(f"  doc:    {payload['doc']}")
    print()
    print(f"  type     {g['type']}")
    print(f"  subject  {g['subject']}")
    markers = ", ".join(f"{k}={v}" for k, v in g["kind_markers"].items())
    print(f"  kind     {markers}")
    print(f"  version  {g['schemaref']}  /  {g['dataschema']}")
    print()
    print("  Versioning lives ONLY in schemaref/dataschema -- never as a token.")
    print("  Identity (repo, agent) lives in data.* and actor.* -- never as a token.")

    _section("DOMAINS", "type segment 2", payload["domains"])
    _section("ENTITIES", "type segment 3", payload["entities"])
    _section("EVENT ACTIONS", "type segment 4 when kind=event (past tense)",
             payload["event_actions"])
    _section("COMMAND ACTIONS",
             "type segment 4 when kind=command or reply (imperative)",
             payload["command_actions"])
    _section("BANNED TOKENS", "never in a type; put these in actor.* (§9)",
             payload["banned_tokens"])
    print()
    print("  Shape-valid is not contract-valid: a 4-token type whose action is")
    print("  absent above is refused. Check a name before you write the producer:")
    print("    bb emit --check --type bloodbank.evt.repo.decision.recorded")
    print("    bb contract --json | jq -r '.event_actions[]'")
    return 0


def cmd_emit(args: argparse.Namespace) -> int:
    """Publish (or, with ``--check``, dry-run) via ``bin/bb-emit``.

    This is a passthrough, not a reimplementation, and that is the whole point.
    ``bin/bb-emit`` is the emitter that n8n, the agent hooks, tiller and
    delonet-daily-report already call; it builds the envelope, derives the
    subject, and runs ``assert_contract`` before publishing. A second envelope
    builder living here would be a second thing to drift -- and two
    half-emitters, one of them a stub that refused to run, is a large part of
    why the contract was undiscoverable in the first place. So ``bb emit`` and
    ``bb-emit`` are one code path, and ``bb emit --check`` is the dry run.

    The prior stub refused unless ``DAPR_HTTP_PORT`` was set, citing ADR-0001.
    ADR-0001 governs *production service* traffic, which flows through Dapr
    publishers inside services; the host-side operator path is ``bb-emit``, over
    NATS, and it has been carrying real traffic for months. Gating the operator
    CLI on a sidecar it was never going to use meant ``bb emit`` did nothing at
    all while its working twin sat in ``bin/`` unmentioned.
    """
    emitter = bloodbank_root() / "bin" / "bb-emit"
    if not emitter.is_file():
        print(f"emit: cannot find emitter at {emitter}", file=sys.stderr)
        return 2
    proc = subprocess.run(  # noqa: S603 -- fixed in-repo interpreter + script
        [sys.executable, str(emitter), *args.emit_args],
        check=False,
    )
    return proc.returncode


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


def _run_gh_readonly_with_retry(root: Path, *argv: str) -> tuple[int, str, str]:
    """Run read-only gh commands with bounded retry on transient connectivity errors."""
    attempts = 3
    rc = 1
    out = ""
    err = ""

    for attempt in range(1, attempts + 1):
        rc, out, err = _run(root, *argv)
        if rc == 0:
            return rc, out, err

        err_lower = err.lower()
        transient = (
            "error connecting to api.github.com" in err_lower
            or "connection reset" in err_lower
            or "timed out" in err_lower
        )
        if not transient or attempt == attempts:
            return rc, out, err

        time.sleep(0.5 * attempt)

    return rc, out, err


def _write_output(path_str: str, content: str) -> str | None:
    """Write command output to ``path_str``. Returns error text on failure."""
    try:
        out_path = Path(path_str)
        if out_path.parent != Path(""):
            out_path.parent.mkdir(parents=True, exist_ok=True)
        if content.endswith("\n"):
            out_path.write_text(content)
        else:
            out_path.write_text(content + "\n")
        return None
    except OSError as exc:
        return f"output_write: ERROR ({exc})"


def _git_ref_path_exists(root: Path, ref: str, rel_path: str) -> bool:
    """Return True when ``ref:rel_path`` exists in git object database."""
    rc, _, _ = _run(root, "git", "cat-file", "-e", f"{ref}:{rel_path}")
    return rc == 0


def _submodule_ignore_mode(root: Path, path: str) -> str:
    """Return the configured .gitmodules ignore mode for a submodule path."""
    key = f"submodule.{path}.ignore"
    rc, out, _ = _run(root, "git", "config", "-f", ".gitmodules", "--get", key)
    if rc != 0:
        return ""
    return out.strip().lower()


def _collect_submodule_gitlink_drifts(root: Path) -> tuple[list[dict[str, str]], str | None]:
    """Return gitlink drift entries for submodules (marker '+' in status output)."""
    rc, out, err = _run(root, "git", "submodule", "status", "--recursive")
    if rc != 0:
        return [], f"submodule_status: ERROR ({err or 'git submodule status failed'})"

    drifts: list[dict[str, str]] = []
    for raw in out.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        marker = line[0]
        if marker != "+":
            continue

        payload = line[1:].strip().split()
        if len(payload) < 2:
            continue

        current_commit = payload[0]
        path = payload[1]
        detail = " ".join(payload[2:]).strip()

        if _submodule_ignore_mode(root, path) == "all":
            continue

        recorded_commit = ""
        rc_tree, tree_out, _ = _run(root, "git", "ls-tree", "HEAD", path)
        if rc_tree == 0 and tree_out:
            # ls-tree format: <mode> <type> <object>\t<path>
            tree_parts = tree_out.split()
            if len(tree_parts) >= 3:
                recorded_commit = tree_parts[2]

        drifts.append(
            {
                "path": path,
                "recorded_commit": recorded_commit,
                "current_commit": current_commit,
                "detail": detail,
            }
        )

    return drifts, None


def cmd_repo_health(args: argparse.Namespace) -> int:
    """Print a concise repo/ticket/PR/check snapshot for BMAD evidence."""
    root = bloodbank_root()

    snapshot: dict[str, object] = {
        "git_status": None,
        "worktree_dirty": None,
        "submodule_gitlink_drifts": [],
        "helper_local_exists": None,
        "helper_on_origin_main": None,
        "issues_open": [],
        "prs_open": [],
        "latest_pr_checks": None,
        "warnings": [],
        "errors": [],
    }

    rc_git, git_out, git_err = _run(root, "git", "status", "--short", "--branch")
    if rc_git != 0:
        err = f"git_status: ERROR ({git_err or 'unknown git error'})"
        if args.json_output:
            snapshot["errors"] = [err]
            rendered_error = json.dumps(snapshot, indent=2)
        else:
            rendered_error = err

        if args.out_path:
            write_err = _write_output(args.out_path, rendered_error)
            if write_err:
                print(rendered_error)
                print(write_err)
                return 2
            return 2

        print(rendered_error)
        return 2

    git_lines = git_out.splitlines()
    git_line = git_lines[0] if git_lines else "<no status output>"
    snapshot["git_status"] = git_line
    snapshot["worktree_dirty"] = len(git_lines) > 1

    drifts, submodule_err = _collect_submodule_gitlink_drifts(root)
    snapshot["submodule_gitlink_drifts"] = drifts
    if submodule_err:
        cast_errors = snapshot["errors"]
        assert isinstance(cast_errors, list)
        cast_errors.append(submodule_err)

    if drifts:
        cast_warnings = snapshot["warnings"]
        assert isinstance(cast_warnings, list)
        cast_warnings.append(
            "submodule_gitlink_drifts: WARN (submodule commit differs from superproject gitlink)"
        )

    helper_rel = "ops/bmad/reconcile_main_divergence.py"
    snapshot["helper_local_exists"] = (root / helper_rel).is_file()
    snapshot["helper_on_origin_main"] = _git_ref_path_exists(root, "origin/main", helper_rel)

    if args.require_clean_worktree and snapshot["worktree_dirty"] is True:
        cast_errors = snapshot["errors"]
        assert isinstance(cast_errors, list)
        cast_errors.append(
            "worktree_dirty: ERROR (working tree is dirty but --require-clean-worktree was set)"
        )

    rc_issue, issue_out, issue_err = _run_gh_readonly_with_retry(
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
            cast_errors = snapshot["errors"]
            assert isinstance(cast_errors, list)
            cast_errors.append("issues_open: ERROR (invalid JSON from gh issue list)")
    elif rc_issue != 0:
        cast_errors = snapshot["errors"]
        assert isinstance(cast_errors, list)
        cast_errors.append(f"issues_open: ERROR ({issue_err or 'gh issue list failed'})")
    snapshot["issues_open"] = issues

    rc_pr, pr_out, pr_err = _run_gh_readonly_with_retry(
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
            cast_errors = snapshot["errors"]
            assert isinstance(cast_errors, list)
            cast_errors.append("prs_open: ERROR (invalid JSON from gh pr list)")
    elif rc_pr != 0:
        cast_errors = snapshot["errors"]
        assert isinstance(cast_errors, list)
        cast_errors.append(f"prs_open: ERROR ({pr_err or 'gh pr list failed'})")
    snapshot["prs_open"] = prs

    if prs:
        newest = sorted(prs, key=lambda x: str(x.get("updatedAt", "")), reverse=True)[0]
        pr_number = int(newest["number"])
        rc_checks, checks_out, checks_err = _run_gh_readonly_with_retry(
            root, "gh", "pr", "checks", str(pr_number)
        )
        if rc_checks in (0, 8) and checks_out:
            snapshot["latest_pr_checks"] = {
                "pr_number": pr_number,
                "lines": checks_out.splitlines(),
            }
        elif rc_checks in (0, 8):
            snapshot["latest_pr_checks"] = {
                "pr_number": pr_number,
                "lines": [],
            }
        else:
            cast_errors = snapshot["errors"]
            assert isinstance(cast_errors, list)
            cast_errors.append(f"latest_pr_checks: ERROR ({checks_err or 'gh pr checks failed'})")
            snapshot["latest_pr_checks"] = {"pr_number": pr_number, "lines": []}
    else:
        snapshot["latest_pr_checks"] = None

    rendered = ""
    if args.json_output:
        rendered = json.dumps(snapshot, indent=2)
    else:
        lines_out: list[str] = []
        lines_out.append(f"git_status: {snapshot['git_status']}")
        lines_out.append(f"worktree_dirty: {str(snapshot['worktree_dirty']).lower()}")

        drifts_out = snapshot["submodule_gitlink_drifts"]
        assert isinstance(drifts_out, list)
        lines_out.append(f"submodule_gitlink_drifts: {len(drifts_out)}")
        for drift in drifts_out:
            lines_out.append(
                "- submodule "
                f"{drift['path']}: recorded={drift.get('recorded_commit', '')} "
                f"current={drift.get('current_commit', '')}"
            )

        lines_out.append(
            "helper_local_exists: "
            f"{str(snapshot['helper_local_exists']).lower()}"
        )
        lines_out.append(
            "helper_on_origin_main: "
            f"{str(snapshot['helper_on_origin_main']).lower()}"
        )

        issues_out = snapshot["issues_open"]
        assert isinstance(issues_out, list)
        lines_out.append(f"issues_open: {len(issues_out)}")
        for it in issues_out:
            lines_out.append(f"- issue #{it['number']}: {it['title']} ({it['url']})")

        prs_out = snapshot["prs_open"]
        assert isinstance(prs_out, list)
        lines_out.append(f"prs_open: {len(prs_out)}")
        for pr in prs_out:
            lines_out.append(f"- pr #{pr['number']}: {pr['title']} ({pr['url']})")

        latest_checks = snapshot["latest_pr_checks"]
        if latest_checks is None:
            lines_out.append("latest_pr_checks: none (no open PRs)")
        else:
            assert isinstance(latest_checks, dict)
            lines_out.append(f"latest_pr_checks: #{latest_checks['pr_number']}")
            check_lines = latest_checks.get("lines", [])
            assert isinstance(check_lines, list)
            for line in check_lines:
                lines_out.append(f"  {line}")

        warnings_out = snapshot["warnings"]
        assert isinstance(warnings_out, list)
        for warn in warnings_out:
            lines_out.append(warn)

        errors_out = snapshot["errors"]
        assert isinstance(errors_out, list)
        for err in errors_out:
            lines_out.append(err)

        rendered = "\n".join(lines_out)

    if args.out_path:
        write_err = _write_output(args.out_path, rendered)
        if write_err:
            cast_errors = snapshot["errors"]
            assert isinstance(cast_errors, list)
            cast_errors.append(write_err)
            print(rendered)
            print(write_err)
        else:
            # Keep stdout quiet when explicit output path is requested.
            pass
    else:
        print(rendered)

    errors = snapshot["errors"]
    assert isinstance(errors, list)
    return 0 if len(errors) == 0 else 1


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bb",
        description=(
            "Bloodbank operator CLI. `bb contract` prints the legal naming "
            "vocabulary; `bb emit --check` dry-runs a name against it before "
            "you write the producer. Everything but a bare `bb emit` is "
            "side-effect free."
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

    p_contract = subparsers.add_parser(
        "contract",
        help="print the legal domains, entities, event actions and command actions",
        description=(
            "Print the naming vocabulary enforced by "
            "services/agent-hooks/core/validate.py. Use --json to consume it "
            "from a generator or an agent skill instead of hardcoding literals."
        ),
    )
    p_contract.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit the vocabulary as JSON for machine consumption",
    )
    p_contract.set_defaults(func=cmd_contract)

    # `emit` is registered only so it appears in `bb --help`; its arguments are
    # never parsed here. main() intercepts `bb emit ...` before argparse runs and
    # hands the tail to bin/bb-emit verbatim -- argparse.REMAINDER captures a
    # LEADING option into the parent parser instead of the subcommand, so
    # `bb emit --check --type X` died on "unrecognized arguments" while
    # `bb emit --type X --check` worked. A passthrough must not have opinions
    # about flag order.
    subparsers.add_parser(
        "emit",
        help="publish an event/command via bin/bb-emit (--check to dry-run)",
        add_help=False,
    )

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
    p_repo_health.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit structured JSON instead of text",
    )
    p_repo_health.add_argument(
        "--out",
        dest="out_path",
        help="optional output file path for snapshot content",
    )
    p_repo_health.add_argument(
        "--require-clean-worktree",
        action="store_true",
        dest="require_clean_worktree",
        help="exit non-zero if the local git working tree is dirty",
    )
    p_repo_health.set_defaults(func=cmd_repo_health)

    p_verify = subparsers.add_parser(
        "verify-envelope",
        help="assert an envelope against the naming contract (docs/event-naming.md)",
        description=(
            "Read a CloudEvents envelope from --file or stdin and assert it "
            "against the Bloodbank Event Naming Contract: type "
            "bloodbank.<domain>.<entity>.<action> (4 tokens), subject "
            "bloodbank.<evt|cmd|rpy>.<domain>.<entity>.<action> (5 tokens), "
            "with every token drawn from the allowlists `bb contract` prints. "
            "There is no version token in either -- versioning lives in "
            "schemaref/dataschema alone."
        ),
    )
    p_verify.add_argument(
        "--file",
        default="-",
        help="path to envelope JSON; '-' or omitted reads from stdin",
    )
    p_verify.set_defaults(func=cmd_verify_envelope)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Passthrough, before argparse can claim any of it. See build_parser().
    if argv and argv[0] == "emit":
        return cmd_emit(argparse.Namespace(emit_args=argv[1:]))

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
