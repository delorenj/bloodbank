#!/usr/bin/env bash
# Smoke test for ops/repo-health/cleanup.py
# Covers default cleanup, KEEP retention, REPORT+DRY_RUN JSON, and invalid KEEP handling.

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${BLOODBANK_ROOT}"

EVIDENCE_DIR="_bmad_output/evidence"
mkdir -p "${EVIDENCE_DIR}"

BACKUP_DIR="$(mktemp -d)"
restore_original=0

cleanup_restore() {
  if [[ "${restore_original}" -eq 1 ]]; then
    find "${EVIDENCE_DIR}" -maxdepth 1 -type f -name 'repo-health-*.json' -delete || true
    if compgen -G "${BACKUP_DIR}/repo-health-*.json" > /dev/null; then
      mv "${BACKUP_DIR}"/repo-health-*.json "${EVIDENCE_DIR}/" || true
    fi
  fi
  rm -rf "${BACKUP_DIR}"
}
trap cleanup_restore EXIT

if compgen -G "${EVIDENCE_DIR}/repo-health-*.json" > /dev/null; then
  restore_original=1
  mv "${EVIDENCE_DIR}"/repo-health-*.json "${BACKUP_DIR}/"
fi

make_artifact() {
  local ts="$1"
  printf '{"ok": true}\n' > "${EVIDENCE_DIR}/repo-health-${ts}.json"
}

count_artifacts() {
  find "${EVIDENCE_DIR}" -maxdepth 1 -type f -name 'repo-health-*.json' | wc -l | tr -d ' '
}

# 1) default cleanup: remove all
make_artifact "20260101T000001Z"
make_artifact "20260101T000002Z"
python3 ops/repo-health/cleanup.py >/dev/null
[[ "$(count_artifacts)" == "0" ]] || { echo "default cleanup failed" >&2; exit 1; }

# 2) KEEP=1 retention mode
make_artifact "20260101T000003Z"
make_artifact "20260101T000004Z"
KEEP=1 python3 ops/repo-health/cleanup.py >/dev/null
[[ "$(count_artifacts)" == "1" ]] || { echo "KEEP=1 cleanup failed" >&2; exit 1; }

# 3) REPORT=1 JSON mode (+ retention)
make_artifact "20260101T000005Z"
report_json="$(REPORT=1 KEEP=1 python3 ops/repo-health/cleanup.py)"
python3 - <<'PY' "${report_json}"
import json
import sys
payload = json.loads(sys.argv[1])
assert payload["removed_count"] == 1, payload
assert payload["kept_count"] == 1, payload
assert len(payload["removed_paths"]) == 1, payload
assert len(payload["kept_paths"]) == 1, payload
PY

# 4) DRY_RUN=1 should not delete files
make_artifact "20260101T000006Z"
before_dry="$(count_artifacts)"
dry_report="$(DRY_RUN=1 REPORT=1 KEEP=1 python3 ops/repo-health/cleanup.py)"
after_dry="$(count_artifacts)"
[[ "${before_dry}" == "${after_dry}" ]] || { echo "DRY_RUN mutated artifacts" >&2; exit 1; }
python3 - <<'PY' "${dry_report}"
import json
import sys
payload = json.loads(sys.argv[1])
assert payload["dry_run"] is True, payload
assert payload["removed_count"] >= 1, payload
assert payload["kept_count"] >= 1, payload
PY

# 5) invalid KEEP should fail non-zero
set +e
KEEP=abc python3 ops/repo-health/cleanup.py >/dev/null 2>&1
rc=$?
set -e
[[ "$rc" -ne 0 ]] || { echo "invalid KEEP did not fail" >&2; exit 1; }

# cleanup after test artifacts
python3 ops/repo-health/cleanup.py >/dev/null

echo "smoketest-repo-health-cleanup: PASS"
