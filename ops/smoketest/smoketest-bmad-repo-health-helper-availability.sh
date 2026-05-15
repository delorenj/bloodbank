#!/usr/bin/env bash
# Deterministic smoke test for repo-health helper availability fields.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import json
from argparse import Namespace
from pathlib import Path

import cli.bb as bb

orig_run = bb._run
orig_root = bb.bloodbank_root

try:
    def fake_root():
        return Path(".").resolve()

    def fake_run(_root, *argv):
        if argv[:3] == ("git", "status", "--short"):
            return (0, "## main...origin/main [ahead 1, behind 1]", "")
        if argv[:4] == ("git", "cat-file", "-e", "origin/main:ops/bmad/reconcile_main_divergence.py"):
            return (0, "", "")
        if argv[:3] == ("gh", "issue", "list"):
            return (0, "[]", "")
        if argv[:3] == ("gh", "pr", "list"):
            return (0, "[]", "")
        return (0, "", "")

    bb.bloodbank_root = fake_root
    bb._run = fake_run

    args = Namespace(limit=5, json_output=True, out_path=None, require_clean_worktree=False)
    rc = bb.cmd_repo_health(args)
    assert rc == 0, rc

    # Re-run and capture rendered text path for non-json fields
    args = Namespace(limit=5, json_output=False, out_path="/tmp/repo-health-helper-smoke.txt", require_clean_worktree=False)
    rc = bb.cmd_repo_health(args)
    assert rc == 0, rc

    txt = Path("/tmp/repo-health-helper-smoke.txt").read_text(encoding="utf-8")
    assert "helper_local_exists:" in txt, txt
    assert "helper_on_origin_main: true" in txt, txt

finally:
    bb._run = orig_run
    bb.bloodbank_root = orig_root
PY

echo "smoketest-bmad-repo-health-helper-availability: PASS"
