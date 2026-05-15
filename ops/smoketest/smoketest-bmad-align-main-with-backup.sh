#!/usr/bin/env bash
# Deterministic smoke test for align_main_with_backup helper.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import subprocess

import ops.bmad.align_main_with_backup as h

orig_run = h._run
orig_branch = h._branch

try:
    # Case 1: read-only evaluation yields backup/reset recommendation.
    def fake_run_eval(_repo, *cmd):
        if cmd[:3] == ("git", "rev-list", "--left-right"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="1 5\n", stderr="")
        if cmd[:4] == ("git", "log", "--left-right", "--cherry-pick"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="< a\n> b\n", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    h._run = fake_run_eval
    h._branch = lambda _repo: "main"

    payload = h.evaluate(h.Path("."), h.Path("/tmp"), timestamp="20260515T220000Z")
    assert payload["ok"] is True, payload
    assert payload["ahead"] == 1 and payload["behind"] == 5, payload
    assert payload["recommended_action"] == "backup_then_reset_origin_main", payload
    assert payload["backup_branch"] == "backup/main-divergence-20260515T220000Z", payload

    # Case 2: apply requires branch=main.
    h._branch = lambda _repo: "feature/test"
    payload_non_main = h.evaluate(h.Path("."), h.Path("/tmp"), timestamp="20260515T220000Z")
    ok, msg = h.apply(h.Path("."), payload_non_main)
    assert ok is False and "branch = main" in msg, (ok, msg)

    # Case 3: apply success path performs branch+bundle+reset.
    calls = []

    def fake_run_apply(_repo, *cmd):
        calls.append(cmd)
        if cmd[:3] == ("git", "rev-list", "--left-right"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="1 2\n", stderr="")
        if cmd[:4] == ("git", "log", "--left-right", "--cherry-pick"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="< a\n> b\n", stderr="")
        if cmd[:2] == ("git", "branch"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if cmd[:3] == ("git", "bundle", "create"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if cmd[:3] == ("git", "reset", "--hard"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    h._run = fake_run_apply
    h._branch = lambda _repo: "main"
    payload_apply = h.evaluate(h.Path("."), h.Path("/tmp"), timestamp="20260515T220001Z")
    ok, msg = h.apply(h.Path("."), payload_apply)
    assert ok is True and msg == "applied", (ok, msg)
    assert payload_apply["backup_created"] is True, payload_apply
    assert payload_apply["bundle_created"] is True, payload_apply
    assert payload_apply["reset_applied"] is True, payload_apply

finally:
    h._run = orig_run
    h._branch = orig_branch
PY

echo "smoketest-bmad-align-main-with-backup: PASS"
