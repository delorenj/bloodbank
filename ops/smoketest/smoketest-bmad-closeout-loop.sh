#!/usr/bin/env bash
# Smoke test for ops/bmad/closeout_loop.py
# Covers machine-readable JSON output and key closeout fields using
# a non-destructive already-merged PR reference.

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${BLOODBANK_ROOT}"

PRIMARY_REPO="${PRIMARY_REPO:-${BLOODBANK_ROOT}}"
MERGED_PR="${MERGED_PR:-113}"

closeout_json="$(python3 ops/bmad/closeout_loop.py "${MERGED_PR}" --primary-repo "${PRIMARY_REPO}")"

python3 - <<'PY' "${closeout_json}"
import json
import sys

payload = json.loads(sys.argv[1])
for key in [
    "overall_status",
    "merged",
    "drift_snapshot_ok",
    "cleanup_followup_commands",
    "merge",
    "drift",
]:
    assert key in payload, payload

assert payload["overall_status"] == "ok", payload
assert payload["merged"] is True, payload
assert payload["drift_snapshot_ok"] is True, payload
assert isinstance(payload["cleanup_followup_commands"], list), payload
assert isinstance(payload["merge"], dict), payload
assert isinstance(payload["drift"], dict), payload
PY

echo "smoketest-bmad-closeout-loop: PASS"
