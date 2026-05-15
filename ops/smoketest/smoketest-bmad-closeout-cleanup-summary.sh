#!/usr/bin/env bash
# Smoke test for ops/bmad/closeout_cleanup_summary.py

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${BLOODBANK_ROOT}"

TMP_EVIDENCE_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "${TMP_EVIDENCE_DIR}"
}
trap cleanup EXIT

cat > "${TMP_EVIDENCE_DIR}/closeout-loop-a.json" <<'JSON'
{
  "pr": 101,
  "overall_status": "ok",
  "merged": true,
  "cleanup_local_branch_status": "already_absent",
  "cleanup_local_branch_deleted": true,
  "cleanup_followup_commands": [],
  "warnings": []
}
JSON

cat > "${TMP_EVIDENCE_DIR}/closeout-loop-b.json" <<'JSON'
{
  "pr": 99,
  "overall_status": "ok",
  "merged": true,
  "merge": {
    "cleanup": {
      "local_branch_status": "failed",
      "local_branch_deleted": false,
      "followup_commands": ["git branch -d fix/issue-99"]
    }
  },
  "warnings": ["local branch cleanup requires follow-up"]
}
JSON

summary_json="$(python3 ops/bmad/closeout_cleanup_summary.py --evidence-dir "${TMP_EVIDENCE_DIR}" --limit 5)"

python3 - <<'PY' "${summary_json}"
import json
import sys

payload = json.loads(sys.argv[1])
assert payload["count"] == 2, payload
assert isinstance(payload["items"], list), payload

items = payload["items"]
first = items[0]
second = items[1]

assert first["cleanup_local_branch_status"] in {"already_absent", "failed"}, payload
assert second["cleanup_local_branch_status"] in {"already_absent", "failed"}, payload

by_pr = {row["pr"]: row for row in items}
assert by_pr[101]["cleanup_local_branch_status"] == "already_absent", payload
assert by_pr[101]["cleanup_followup_count"] == 0, payload
assert by_pr[99]["cleanup_local_branch_status"] == "failed", payload
assert by_pr[99]["cleanup_followup_count"] == 1, payload
assert by_pr[99]["warning_count"] == 1, payload
PY

echo "smoketest-bmad-closeout-cleanup-summary: PASS"
