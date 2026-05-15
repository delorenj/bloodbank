#!/usr/bin/env bash
# Smoke test for closeout artifact writing + cleanup summary visibility.

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${BLOODBANK_ROOT}"

MERGED_PR="${MERGED_PR:-139}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

OUT_PATH="${TMP_DIR}/closeout-loop-test.json"
python3 ops/bmad/closeout_loop.py "${MERGED_PR}" --out "${OUT_PATH}" >/tmp/closeout-loop-smoke.json

[[ -f "${OUT_PATH}" ]] || { echo "missing closeout artifact" >&2; exit 1; }

summary_json="$(python3 ops/bmad/closeout_cleanup_summary.py --evidence-dir "${TMP_DIR}" --limit 5)"
python3 - <<'PY' "${summary_json}" "${OUT_PATH}"
import json
import sys

payload = json.loads(sys.argv[1])
out_path = sys.argv[2]

assert payload["count"] == 1, payload
item = payload["items"][0]
assert item["artifact"] == out_path, payload
assert item["cleanup_local_branch_status"] in {"already_absent", "deleted", "failed", "not_applicable", "unknown"}, payload
PY

echo "smoketest-bmad-closeout-artifact-summary: PASS"
