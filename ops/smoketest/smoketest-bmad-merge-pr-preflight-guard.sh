#!/usr/bin/env bash
# Deterministic smoke test for merge_pr_safe strict-clean preflight enforcement.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import contextlib
import io
import json
import sys

import ops.bmad.merge_pr_safe as m

orig_run_preflight = m.run_preflight
orig_run = m.run
orig_gh_pr_view = m.gh_pr_view
orig_branch_exists = m.branch_exists_local
orig_argv = sys.argv[:]

try:
    # Case 1: preflight fails and blocks merge when no bypass flag provided.
    m.run_preflight = lambda _repo: (1, {"ok": False, "blocking_reason": "worktree_dirty"})
    sys.argv = ["merge_pr_safe.py", "123"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = m.main()
    payload = json.loads(buf.getvalue())
    assert rc == 1, (rc, payload)
    assert payload["state"] == "BLOCKED", payload
    assert payload["preflight"]["blocking_reason"] == "worktree_dirty", payload

    # Case 2: bypass flag skips preflight and proceeds through merge contract.
    m.run_preflight = lambda _repo: (_ for _ in ()).throw(RuntimeError("should not be called"))
    m.run = lambda *_args: m.CmdResult(code=0, out="", err="")
    m.gh_pr_view = lambda _pr: {
        "number": 123,
        "state": "MERGED",
        "mergedAt": "2026-01-01T00:00:00Z",
        "url": "https://example.test/pr/123",
        "headRefName": "fix/branch",
    }
    m.branch_exists_local = lambda _branch: False

    sys.argv = ["merge_pr_safe.py", "123", "--bypass-preflight"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = m.main()
    payload = json.loads(buf.getvalue())
    assert rc == 0, (rc, payload)
    assert payload["state"] == "MERGED", payload
    assert payload["preflight"]["bypassed"] is True, payload

finally:
    m.run_preflight = orig_run_preflight
    m.run = orig_run
    m.gh_pr_view = orig_gh_pr_view
    m.branch_exists_local = orig_branch_exists
    sys.argv = orig_argv
PY

echo "smoketest-bmad-merge-pr-preflight-guard: PASS"
