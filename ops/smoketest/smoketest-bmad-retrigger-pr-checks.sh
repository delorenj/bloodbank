#!/usr/bin/env bash
# Smoke test for ops/bmad/retrigger_pr_checks.py
# Covers machine-readable JSON output contract in dry-run mode.

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${BLOODBANK_ROOT}"

PR_NUMBER="${PR_NUMBER:-127}"

retrigger_json="$(python3 ops/bmad/retrigger_pr_checks.py "${PR_NUMBER}" --dry-run)"

python3 - <<'PY' "${retrigger_json}" "${PR_NUMBER}"
import json
import sys

payload = json.loads(sys.argv[1])
pr_number = int(sys.argv[2])

for key in [
    "repository",
    "pr",
    "pr_state",
    "pr_url",
    "head_ref",
    "workflow",
    "dry_run",
    "dispatch_requested",
    "dispatch_exit",
    "dispatch_stderr",
    "followup_commands",
]:
    assert key in payload, payload

assert payload["pr"] == pr_number, payload
assert payload["workflow"] == "ci.yml", payload
assert payload["dry_run"] is True, payload
assert payload["dispatch_requested"] is False, payload
assert payload["dispatch_exit"] is None, payload
assert isinstance(payload["followup_commands"], list), payload
assert payload["followup_commands"], payload
PY

echo "smoketest-bmad-retrigger-pr-checks: PASS"
