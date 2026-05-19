#!/usr/bin/env bash
# Local smoke test for submodule drift reconcile helper.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from pathlib import Path

import ops.bmad.reconcile_submodule_gitlink_drift as m

repo = Path('.').resolve()
orig_run = m._run
orig_branch = m._branch
orig_status_lines = m._status_lines

class CP:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

try:
    # evaluate(): detects one drift and records commits.
    def fake_run_eval(_repo, *cmd):
        if cmd[:4] == ('git', 'submodule', 'status', '--recursive'):
            return CP(0, '+2b1061b012511ad46d7449ab0ac82f4fb595f135 agents/hermes/pm/runtime (heads/main)\n')
        if cmd[:3] == ('git', 'ls-tree', 'HEAD'):
            return CP(0, '160000 commit 65a0c089c3e1d10ee6a722bef076ee9a0646ab63\tagents/hermes/pm/runtime\n')
        if cmd[:3] == ('git', 'rev-parse', '--abbrev-ref'):
            return CP(0, 'main\n')
        return CP(0, '')

    m._run = fake_run_eval
    payload = m.evaluate(repo)
    assert payload['ok'] is True, payload
    assert payload['drift_count'] == 1, payload
    drifts = payload['drifts']
    assert isinstance(drifts, list) and drifts[0]['path'] == 'agents/hermes/pm/runtime', payload

    # apply_if_safe(): blocks off-main.
    m._branch = lambda _repo: 'feature/test'
    ok, msg = m.apply_if_safe(repo, payload)
    assert ok is False and 'current branch = main' in msg, (ok, msg)

    # apply_if_safe(): blocks extra dirty paths.
    m._branch = lambda _repo: 'main'
    m._status_lines = lambda _repo: [' M agents/hermes/pm/runtime', ' M cli/bb.py']
    ok, msg = m.apply_if_safe(repo, payload)
    assert ok is False and 'clean worktree except listed drift paths' in msg, (ok, msg)

    # apply_if_safe(): success path executes checkout.
    calls = []

    def fake_run_apply(_repo, *cmd):
        calls.append(cmd)
        if cmd[:3] == ('git', 'submodule', 'status'):
            return CP(0, '+2b1061b012511ad46d7449ab0ac82f4fb595f135 agents/hermes/pm/runtime (heads/main)\n')
        if cmd[:3] == ('git', 'ls-tree', 'HEAD'):
            return CP(0, '160000 commit 65a0c089c3e1d10ee6a722bef076ee9a0646ab63\tagents/hermes/pm/runtime\n')
        if cmd[:3] == ('git', 'rev-parse', '--abbrev-ref'):
            return CP(0, 'main\n')
        if cmd[:3] == ('git', '-C', 'agents/hermes/pm/runtime'):
            return CP(0, '')
        return CP(0, '')

    m._run = fake_run_apply
    m._status_lines = lambda _repo: [' M agents/hermes/pm/runtime']
    ok, msg = m.apply_if_safe(repo, payload)
    assert ok is True and msg == 'applied', (ok, msg)
    assert any(cmd[:3] == ('git', '-C', 'agents/hermes/pm/runtime') for cmd in calls), calls

finally:
    m._run = orig_run
    m._branch = orig_branch
    m._status_lines = orig_status_lines
PY

echo "smoketest-bmad-reconcile-submodule-drift: PASS"
