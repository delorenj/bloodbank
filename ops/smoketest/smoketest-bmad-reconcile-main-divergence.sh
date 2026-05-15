#!/usr/bin/env bash
# Deterministic smoke test for reconcile_main_divergence helper.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import subprocess

import ops.bmad.reconcile_main_divergence as h

orig_run = h._run
orig_branch = h._branch

try:
    # Simulate patch-equivalent divergence (ahead 1 / behind 1, empty cherry output)
    calls = []

    def fake_run(_repo, *cmd):
        calls.append(cmd)
        if cmd[:3] == ("git", "rev-list", "--left-right"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="1 1\n", stderr="")
        if cmd[:4] == ("git", "log", "--left-right", "--cherry-pick"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    h._run = fake_run
    h._branch = lambda _repo: "main"

    payload = h.evaluate(h.Path("."))
    assert payload["ok"] is True, payload
    assert payload["ahead"] == 1 and payload["behind"] == 1, payload
    assert payload["patch_equivalent_divergence"] is True, payload
    assert "reset --hard origin/main" in str(payload["recommended_action"]), payload

    # Simulate safe apply success
    def fake_run_apply(_repo, *cmd):
        if cmd[:3] == ("git", "rev-list", "--left-right"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="1 1\n", stderr="")
        if cmd[:4] == ("git", "log", "--left-right", "--cherry-pick"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if cmd[:3] == ("git", "reset", "--hard"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    h._run = fake_run_apply
    ok, msg = h.apply_if_safe(h.Path("."), payload)
    assert ok is True and msg == "applied", (ok, msg)

finally:
    h._run = orig_run
    h._branch = orig_branch
PY

echo "smoketest-bmad-reconcile-main-divergence: PASS"
