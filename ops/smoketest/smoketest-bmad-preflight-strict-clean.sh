#!/usr/bin/env bash
# Deterministic smoke test for strict-clean preflight helper.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import json
import subprocess

import ops.bmad.preflight_strict_clean as p

orig = p._run_repo_health_strict

try:
    # clean repo path => success
    def fake_clean(_repo):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "git_status": "## main...origin/main",
                    "worktree_dirty": False,
                    "errors": [],
                }
            ),
            stderr="",
        )

    p._run_repo_health_strict = fake_clean
    rc, payload = p.evaluate(p.Path("."))
    assert rc == 0, (rc, payload)
    assert payload["ok"] is True, payload
    assert payload["worktree_dirty"] is False, payload

    # dirty repo path => fail with actionable blocker
    def fake_dirty(_repo):
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(
                {
                    "git_status": "## main...origin/main",
                    "worktree_dirty": True,
                    "errors": [
                        "worktree_dirty: ERROR (working tree is dirty but --require-clean-worktree was set)"
                    ],
                }
            ),
            stderr="",
        )

    p._run_repo_health_strict = fake_dirty
    rc, payload = p.evaluate(p.Path("."))
    assert rc == 1, (rc, payload)
    assert payload["ok"] is False, payload
    assert payload["blocking_reason"] == "worktree_dirty", payload
    assert payload["worktree_dirty"] is True, payload

finally:
    p._run_repo_health_strict = orig
PY

echo "smoketest-bmad-preflight-strict-clean: PASS"
