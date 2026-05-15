#!/usr/bin/env bash
# Smoke test for cli/bb.py transient gh retry helper behavior.

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${BLOODBANK_ROOT}"

python3 - <<'PY'
import cli.bb as bb

orig_run = bb._run
orig_sleep = bb.time.sleep

try:
    # Case 1: transient failures retry then succeed.
    calls = []
    sleeps = []
    responses = [
        (1, "", "error connecting to api.github.com"),
        (1, "", "timed out"),
        (0, "ok", ""),
    ]

    def fake_run_transient(root, *argv):
        calls.append(argv)
        return responses.pop(0)

    bb._run = fake_run_transient
    bb.time.sleep = lambda sec: sleeps.append(sec)

    rc, out, err = bb._run_gh_readonly_with_retry(bb.bloodbank_root(), "gh", "issue", "list")
    assert rc == 0 and out == "ok" and err == "", (rc, out, err)
    assert len(calls) == 3, len(calls)
    assert sleeps == [0.5, 1.0], sleeps

    # Case 2: non-transient errors should not retry.
    calls = []
    sleeps = []

    def fake_run_non_transient(root, *argv):
        calls.append(argv)
        return (1, "", "GraphQL: Projects (classic) is being deprecated")

    bb._run = fake_run_non_transient
    bb.time.sleep = lambda sec: sleeps.append(sec)

    rc, out, err = bb._run_gh_readonly_with_retry(bb.bloodbank_root(), "gh", "pr", "list")
    assert rc == 1 and "deprecated" in err.lower(), (rc, out, err)
    assert len(calls) == 1, len(calls)
    assert sleeps == [], sleeps

    # Case 3: transient errors exhausted at retry bound.
    calls = []
    sleeps = []

    def fake_run_exhausted(root, *argv):
        calls.append(argv)
        return (1, "", "error connecting to api.github.com")

    bb._run = fake_run_exhausted
    bb.time.sleep = lambda sec: sleeps.append(sec)

    rc, out, err = bb._run_gh_readonly_with_retry(bb.bloodbank_root(), "gh", "pr", "checks", "1")
    assert rc == 1 and "error connecting" in err.lower(), (rc, out, err)
    assert len(calls) == 3, len(calls)
    assert sleeps == [0.5, 1.0], sleeps

finally:
    bb._run = orig_run
    bb.time.sleep = orig_sleep
PY

echo "smoketest-bmad-repo-health-retry: PASS"
