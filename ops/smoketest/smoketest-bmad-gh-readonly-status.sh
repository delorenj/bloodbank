#!/usr/bin/env bash
# Smoke test for ops/bmad/gh_readonly_status.py retry semantics.

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${BLOODBANK_ROOT}"

python3 - <<'PY'
import contextlib
import io
import json
import sys

import ops.bmad.gh_readonly_status as s

orig_run_once = s._run_once
orig_sleep = s.time.sleep
orig_argv = sys.argv[:]

try:
    # transient -> success on retry
    attempts = []
    sleeps = []
    responses = [
        (1, "", "error connecting to api.github.com"),
        (1, "", "timed out"),
        (0, '{"state":"OPEN"}', ""),
    ]

    def fake_once(_argv):
        attempts.append(1)
        return responses.pop(0)

    s._run_once = fake_once
    s.time.sleep = lambda sec: sleeps.append(sec)
    rc, out, err, tries = s.run_with_retry(["gh", "issue", "view", "1"])
    assert rc == 0 and tries == 3 and 'OPEN' in out and err == "", (rc, out, err, tries)
    assert sleeps == [0.5, 1.0], sleeps

    # non-transient should not retry
    attempts = []
    sleeps = []

    def fake_non(_argv):
        attempts.append(1)
        return (1, "", "GraphQL: Projects (classic) is being deprecated")

    s._run_once = fake_non
    s.time.sleep = lambda sec: sleeps.append(sec)
    rc, out, err, tries = s.run_with_retry(["gh", "pr", "view", "2"])
    assert rc == 1 and tries == 1 and "deprecated" in err.lower(), (rc, out, err, tries)
    assert sleeps == [], sleeps

    def run_main_case(argv, mocked_response):
        observed = []

        def fake_cmd(cmd_argv):
            observed.append(cmd_argv)
            return mocked_response

        s._run_once = fake_cmd
        s.time.sleep = lambda _sec: None
        sys.argv = argv
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            exit_code = s.main()
        payload = json.loads(stream.getvalue())
        return exit_code, payload, observed

    # issue-view command contract (no network; mocked gh response)
    exit_code, payload, observed = run_main_case(
        ["gh_readonly_status.py", "issue-view", "154"],
        (0, '{"number":154,"title":"T","state":"OPEN","url":"u","updatedAt":"now"}', ""),
    )
    assert exit_code == 0, exit_code
    assert observed == [["gh", "issue", "view", "154", "--json", "number,title,state,url,updatedAt"]], observed
    assert payload["ok"] is True, payload
    assert payload["command"] == "issue-view", payload
    assert payload["data"]["number"] == 154, payload
    assert payload["data"]["state"] == "OPEN", payload

    # pr-view command contract (no network; mocked gh response)
    exit_code, payload, observed = run_main_case(
        ["gh_readonly_status.py", "pr-view", "151"],
        (
            0,
            '{"number":151,"title":"P","state":"OPEN","url":"u","headRefName":"h","mergeStateStatus":"CLEAN","statusCheckRollup":[],"mergedAt":null,"updatedAt":"now"}',
            "",
        ),
    )
    assert exit_code == 0, exit_code
    assert observed == [["gh", "pr", "view", "151", "--json", "number,title,state,url,headRefName,mergeStateStatus,statusCheckRollup,mergedAt,updatedAt"]], observed
    assert payload["ok"] is True, payload
    assert payload["command"] == "pr-view", payload
    assert payload["data"]["number"] == 151, payload
    assert payload["data"]["headRefName"] == "h", payload

    # repo-view command contract (no network; mocked gh response)
    exit_code, payload, observed = run_main_case(
        ["gh_readonly_status.py", "repo-view"],
        (0, '{"nameWithOwner":"delorenj/bloodbank"}', ""),
    )
    assert exit_code == 0, exit_code
    assert observed == [["gh", "repo", "view", "--json", "nameWithOwner"]], observed
    assert payload["ok"] is True, payload
    assert payload["command"] == "repo-view", payload
    assert payload["data"]["nameWithOwner"] == "delorenj/bloodbank", payload

finally:
    s._run_once = orig_run_once
    s.time.sleep = orig_sleep
    sys.argv = orig_argv
PY

echo "smoketest-bmad-gh-readonly-status: PASS"
