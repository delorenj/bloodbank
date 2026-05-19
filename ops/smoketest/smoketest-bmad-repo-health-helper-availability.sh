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
        if argv[:4] == ("git", "submodule", "status", "--recursive"):
            return (
                0,
                "+2b1061b012511ad46d7449ab0ac82f4fb595f135 agents/hermes/pm/runtime (heads/main)",
                "",
            )
        if argv[:3] == ("git", "ls-tree", "HEAD"):
            return (
                0,
                "160000 commit 65a0c089c3e1d10ee6a722bef076ee9a0646ab63\tagents/hermes/pm/runtime",
                "",
            )
        if argv[:4] == ("git", "cat-file", "-e", "origin/main:ops/bmad/reconcile_main_divergence.py"):
            return (0, "", "")
        if argv[:3] == ("gh", "issue", "list"):
            return (0, "[]", "")
        if argv[:3] == ("gh", "pr", "list"):
            return (0, "[]", "")
        return (0, "", "")

    bb.bloodbank_root = fake_root
    bb._run = fake_run

    args = Namespace(
        limit=5,
        json_output=True,
        out_path="/tmp/repo-health-helper-smoke.json",
        require_clean_worktree=False,
    )
    rc = bb.cmd_repo_health(args)
    assert rc == 1, rc  # drift warning is emitted as non-zero evidence signal

    payload = json.loads(Path("/tmp/repo-health-helper-smoke.json").read_text(encoding="utf-8"))
    drifts = payload.get("submodule_gitlink_drifts", [])
    assert len(drifts) == 1, payload
    assert drifts[0]["path"] == "agents/hermes/pm/runtime", payload
    assert drifts[0]["recorded_commit"] == "65a0c089c3e1d10ee6a722bef076ee9a0646ab63", payload
    assert drifts[0]["current_commit"] == "2b1061b012511ad46d7449ab0ac82f4fb595f135", payload

    # Re-run and capture rendered text path for non-json fields
    args = Namespace(limit=5, json_output=False, out_path="/tmp/repo-health-helper-smoke.txt", require_clean_worktree=False)
    rc = bb.cmd_repo_health(args)
    assert rc == 1, rc

    txt = Path("/tmp/repo-health-helper-smoke.txt").read_text(encoding="utf-8")
    assert "helper_local_exists:" in txt, txt
    assert "helper_on_origin_main: true" in txt, txt
    assert "submodule_gitlink_drifts: 1" in txt, txt

finally:
    bb._run = orig_run
    bb.bloodbank_root = orig_root
PY

echo "smoketest-bmad-repo-health-helper-availability: PASS"
