#!/usr/bin/env python3
"""Detect and optionally reconcile submodule gitlink drift.

Default mode is read-only. Use --apply to check out each drifted submodule to the
commit recorded by the superproject gitlink.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

RUNTIME_FORCE_CHECKOUT_PATHS = {"agents/hermes/pm/runtime"}
BMAD_EVIDENCE_PATH_PREFIXES = ("_bmad_output",)


def _run(repo: Path, *cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(repo), text=True, capture_output=True, check=False)


def _branch(repo: Path) -> str:
    cp = _run(repo, "git", "rev-parse", "--abbrev-ref", "HEAD")
    return cp.stdout.strip() if cp.returncode == 0 else ""


def _submodule_ignore_by_path(repo: Path) -> dict[str, str]:
    cp = _run(repo, "git", "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.(path|ignore)$")
    if cp.returncode != 0:
        return {}

    name_to_path: dict[str, str] = {}
    name_to_ignore: dict[str, str] = {}

    for raw in cp.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        key, _, value = line.partition(" ")
        if not key or not value:
            continue
        if key.endswith(".path"):
            name = key[len("submodule.") : -len(".path")]
            name_to_path[name] = value.strip()
        elif key.endswith(".ignore"):
            name = key[len("submodule.") : -len(".ignore")]
            name_to_ignore[name] = value.strip()

    out: dict[str, str] = {}
    for name, path in name_to_path.items():
        ignore_mode = name_to_ignore.get(name)
        if ignore_mode:
            out[path] = ignore_mode
    return out


def _collect_drifts(repo: Path) -> tuple[list[dict[str, str]], list[str]]:
    errors: list[str] = []
    ignore_by_path = _submodule_ignore_by_path(repo)

    cp = _run(repo, "git", "submodule", "status", "--recursive")
    if cp.returncode != 0:
        return [], [cp.stderr.strip() or "git submodule status failed"]

    drifts: list[dict[str, str]] = []
    for raw in cp.stdout.splitlines():
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

        if ignore_by_path.get(path) == "all":
            continue

        recorded_commit = ""
        tree = _run(repo, "git", "ls-tree", "HEAD", path)
        if tree.returncode == 0 and tree.stdout.strip():
            parts = tree.stdout.split()
            if len(parts) >= 3:
                recorded_commit = parts[2]

        if not recorded_commit:
            errors.append(f"missing recorded gitlink commit for submodule path: {path}")

        drifts.append(
            {
                "path": path,
                "recorded_commit": recorded_commit,
                "current_commit": current_commit,
                "detail": detail,
            }
        )

    return drifts, errors


def _status_lines(repo: Path) -> list[str]:
    cp = _run(repo, "git", "status", "--short")
    if cp.returncode != 0 or not cp.stdout.strip():
        return []
    return [line.rstrip() for line in cp.stdout.splitlines() if line.strip()]


def evaluate(repo: Path) -> dict[str, object]:
    drifts, drift_errors = _collect_drifts(repo)

    payload: dict[str, object] = {
        "ok": len(drift_errors) == 0,
        "repo": str(repo),
        "branch": _branch(repo),
        "drift_count": len(drifts),
        "drifts": drifts,
        "recommended_action": (
            "none"
            if len(drifts) == 0
            else "python3 ops/bmad/reconcile_submodule_gitlink_drift.py --repo <repo> --apply"
        ),
        "applied": False,
        "errors": drift_errors,
    }
    return payload


def _safe_to_apply(repo: Path, drift_paths: set[str]) -> tuple[bool, str]:
    if _branch(repo) != "main":
        return False, "apply requires current branch = main"

    lines = _status_lines(repo)
    disallowed: list[str] = []
    for line in lines:
        # status format: XY<space>path
        if len(line) < 4:
            continue
        path = line[3:]
        if any(path == prefix or path.startswith(f"{prefix}/") for prefix in BMAD_EVIDENCE_PATH_PREFIXES):
            continue
        if path not in drift_paths:
            disallowed.append(line)

    if disallowed:
        return False, f"apply requires clean worktree except listed drift paths; found: {disallowed}"

    return True, "ok"


def _runtime_force_checkout_allowed(path: str, stderr: str) -> bool:
    if path not in RUNTIME_FORCE_CHECKOUT_PATHS:
        return False
    lower = (stderr or "").lower()
    return "would be overwritten by checkout" in lower and "state.db" in lower


def apply_if_safe(repo: Path, payload: dict[str, object]) -> tuple[bool, str]:
    drifts = payload.get("drifts")
    assert isinstance(drifts, list)

    drift_paths = {str(d.get("path", "")) for d in drifts if str(d.get("path", ""))}
    ok, msg = _safe_to_apply(repo, drift_paths)
    if not ok:
        return False, msg

    for drift in drifts:
        path = str(drift.get("path", ""))
        recorded = str(drift.get("recorded_commit", ""))
        if not path or not recorded:
            return False, f"invalid drift entry: {drift}"

        cp = _run(repo, "git", "-C", path, "checkout", "--detach", recorded)
        if cp.returncode == 0:
            continue

        stderr = cp.stderr.strip()
        if _runtime_force_checkout_allowed(path, stderr):
            retry = _run(repo, "git", "-C", path, "checkout", "--detach", "-f", recorded)
            if retry.returncode == 0:
                continue
            stderr = retry.stderr.strip() or stderr

        return False, stderr or f"failed to set {path} to {recorded}"

    return True, "applied"


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect/reconcile submodule gitlink drift")
    parser.add_argument("--repo", default=".", help="Repo root (default: .)")
    parser.add_argument("--apply", action="store_true", help="Apply safe reconciliation when eligible")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    payload = evaluate(repo)

    if args.apply:
        ok, msg = apply_if_safe(repo, payload)
        payload["applied"] = ok
        if not ok:
            errs = payload.get("errors")
            if isinstance(errs, list):
                errs.append(msg)
            else:
                payload["errors"] = [msg]
            print(json.dumps(payload, indent=2))
            return 1

        payload = evaluate(repo)
        payload["applied"] = True

    print(json.dumps(payload, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
