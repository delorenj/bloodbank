#!/usr/bin/env bash
# Deterministic smoke test for primary_recovery_check helper.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import subprocess

import ops.bmad.primary_recovery_check as h

orig_run = h._run
orig_exists = h._exists_on_ref

try:
    def fake_run(_repo, *cmd):
        if cmd[:3] == ("git", "status", "--short"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="## main...origin/main [ahead 1, behind 3]\n", stderr="")
        if cmd[:3] == ("git", "rev-parse", "--abbrev-ref"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="main\n", stderr="")
        if cmd[:3] == ("git", "rev-list", "--left-right"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="1 3\n", stderr="")
        if cmd[:4] == ("git", "log", "--left-right", "--cherry-pick"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="< a\n> b\n", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    h._run = fake_run
    h._exists_on_ref = lambda _repo, _ref_path: True

    payload = h.evaluate(h.Path("."))
    assert payload["ok"] is True, payload
    assert payload["ahead"] == 1 and payload["behind"] == 3, payload
    assert payload["patch_equivalent_divergence"] is False, payload
    assert payload["helper_on_origin_main"] is True, payload
    assert payload["recommended_path"] == "manual_rebase_or_backup_then_reset", payload

finally:
    h._run = orig_run
    h._exists_on_ref = orig_exists
PY

echo "smoketest-bmad-primary-recovery-check: PASS"
