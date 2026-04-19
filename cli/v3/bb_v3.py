"""Bloodbank v3 operator CLI scaffold.

Holyfields owns schemas and generated contracts.
Bloodbank owns runtime operations, tracing, replay, and operator tooling.

This module stays safe by design:
- stdlib only
- local static checks only for ``doctor``
- no real publish path for ``emit`` yet
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_FILES = (
    "v3-implementation-plan.md",
    "cli/v3/README.md",
    "cli/v3/bb_v3.py",
    "ops/v3/bootstrap/README.md",
    "ops/v3/bootstrap/check-platform.sh",
    "compose/v3/README.md",
    "compose/v3/nats/README.md",
    "compose/v3/apicurio/README.md",
    "compose/v3/eventcatalog/README.md",
)


@dataclass(frozen=True)
class CheckResult:
    label: str
    ok: bool
    detail: str


def _relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _format_result(result: CheckResult) -> str:
    status = "PASS" if result.ok else "FAIL"
    return f"[{status}] {result.label}: {result.detail}"


def _file_check(path: Path) -> CheckResult:
    rel = _relative_path(path)
    if path.exists():
        return CheckResult("required file", True, f"{rel} exists")
    return CheckResult("required file", False, f"{rel} is missing")


def _source_check(path: Path) -> CheckResult:
    rel = _relative_path(path)
    try:
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))
    except FileNotFoundError:
        return CheckResult("python syntax", False, f"{rel} is missing")
    except SyntaxError as exc:
        return CheckResult(
            "python syntax",
            False,
            f"{rel} does not parse cleanly at line {exc.lineno}: {exc.msg}",
        )
    except UnicodeDecodeError as exc:
        return CheckResult("python syntax", False, f"{rel} is not readable as UTF-8: {exc}")
    return CheckResult("python syntax", True, f"{rel} parses with the local interpreter")


def _doctor_checks() -> list[CheckResult]:
    checks = [_file_check(REPO_ROOT / relative) for relative in EXPECTED_FILES]
    checks.append(_source_check(Path(__file__).resolve()))
    return checks


def _print_results(results: Iterable[CheckResult]) -> int:
    failures = 0
    for result in results:
        print(_format_result(result))
        if not result.ok:
            failures += 1
    if failures:
        print(
            "[ACTION] Fix the missing local scaffold files before wiring any runtime publish path."
        )
        return 1
    print(
        "[PASS] Local v3 scaffold checks completed. No Docker, Dapr, NATS, or network activity was performed."
    )
    return 0


def run_doctor() -> int:
    return _print_results(_doctor_checks())


def _stub_command(name: str) -> int:
    print(f"[PASS] {name} is a safe v3 scaffold stub.")
    print(
        "[ACTION] Bloodbank owns the runtime and operator workflow; Holyfields owns schemas and generated contracts."
    )
    print("[ACTION] This command does not publish real traffic yet.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bb-v3",
        description=(
            "Safe Bloodbank v3 operator CLI scaffold. "
            "Holyfields owns schemas; Bloodbank owns runtime and ops."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "doctor",
        help="Run local static checks only.",
        description="Run local static checks only. No production side effects.",
    )

    trace = subparsers.add_parser(
        "trace",
        help="Trace placeholder for future v3 runtime inspection.",
        description="Safe placeholder for future tracing workflows.",
    )
    trace.add_argument("--correlation-id", default="", help="Correlation identifier to inspect.")
    trace.add_argument("--limit", type=int, default=20, help="Maximum number of records to show.")

    replay = subparsers.add_parser(
        "replay",
        help="Replay placeholder for future stream tooling.",
        description="Safe placeholder for future replay workflows.",
    )
    replay.add_argument("--stream", default="", help="Stream name to replay from.")
    replay.add_argument("--since", default="", help="Lower bound timestamp or cursor.")
    replay.add_argument("--until", default="", help="Upper bound timestamp or cursor.")

    emit = subparsers.add_parser(
        "emit",
        help="Emit placeholder that never publishes real traffic.",
        description="Safe placeholder for future emit workflows. No real publish path yet.",
    )
    emit.add_argument("--subject", default="", help="Subject that would be targeted later.")
    emit.add_argument("--payload", default="", help="Payload that would be sent later.")
    emit.add_argument(
        "--dry-run",
        action="store_true",
        help="Acknowledge that the scaffold does not publish anything yet.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return run_doctor()
    if args.command == "trace":
        return _stub_command("trace")
    if args.command == "replay":
        return _stub_command("replay")
    if args.command == "emit":
        return _stub_command("emit")

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
