#!/usr/bin/env bash
# Smoke test for ops/bmad/closeout_loop.py
# Covers machine-readable JSON output plus primary-repo fallback precedence
# and invalid-path guard using non-destructive already-merged PR references.

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${BLOODBANK_ROOT}"

MERGED_PR="${MERGED_PR:-123}"

# 1) CLI precedence over env
cli_json="$(PRIMARY_REPO=/tmp/not-a-repo python3 ops/bmad/closeout_loop.py "${MERGED_PR}" --primary-repo "${BLOODBANK_ROOT}")"
python3 - <<'PY' "${cli_json}"
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["overall_status"] == "ok", payload
assert payload["merged"] is True, payload
assert payload["drift_snapshot_ok"] is True, payload
assert payload["primary_repo_source"] == "cli", payload
assert isinstance(payload["cleanup_followup_commands"], list), payload
assert isinstance(payload["merge"], dict), payload
assert isinstance(payload["drift"], dict), payload
PY

# 2) Env precedence when CLI omitted
env_json="$(PRIMARY_REPO="${BLOODBANK_ROOT}" python3 ops/bmad/closeout_loop.py "${MERGED_PR}")"
python3 - <<'PY' "${env_json}"
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["overall_status"] == "ok", payload
assert payload["primary_repo_source"] == "env:PRIMARY_REPO", payload
PY

# 3) CWD fallback when neither CLI nor env is set
cwd_json="$(env -u PRIMARY_REPO python3 ops/bmad/closeout_loop.py "${MERGED_PR}")"
python3 - <<'PY' "${cwd_json}" "${BLOODBANK_ROOT}"
import json
import pathlib
import sys

payload = json.loads(sys.argv[1])
expected_repo = str(pathlib.Path(sys.argv[2]).resolve())
assert payload["overall_status"] == "ok", payload
assert payload["primary_repo_source"] == "cwd", payload
assert payload["primary_repo"] == expected_repo, payload
PY

# 4) Invalid resolved primary repo -> error guard
invalid_out="$(mktemp)"
set +e
PRIMARY_REPO=/tmp/not-a-repo python3 ops/bmad/closeout_loop.py "${MERGED_PR}" >"${invalid_out}"
invalid_rc=$?
set -e
if [[ ${invalid_rc} -eq 0 ]]; then
  echo "expected non-zero exit for invalid primary-repo path" >&2
  exit 1
fi

python3 - <<'PY' "${invalid_out}"
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
assert payload["overall_status"] == "error", payload
assert payload["primary_repo_source"] == "env:PRIMARY_REPO", payload
assert "warnings" in payload and payload["warnings"], payload
assert any("not a git worktree" in str(w) for w in payload["warnings"]), payload
PY

rm -f "${invalid_out}"

echo "smoketest-bmad-closeout-loop: PASS"
