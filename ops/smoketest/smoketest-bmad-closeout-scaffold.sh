#!/usr/bin/env bash
# Smoke test for ops/bmad/scaffold_closeout.py
# Covers required ISSUE_ID validation, successful scaffold creation,
# and no-overwrite protection when target file already exists.

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${BLOODBANK_ROOT}"

TMP_ISSUE_ID="99998"
TMP_CLOSEOUT_PATH="_bmad_output/issue-${TMP_ISSUE_ID}-execution.md"

cleanup() {
  rm -f "${TMP_CLOSEOUT_PATH}" || true
}
trap cleanup EXIT

rm -f "${TMP_CLOSEOUT_PATH}"

# 1) missing ISSUE_ID should fail non-zero with clear error
set +e
missing_err="$(python3 ops/bmad/scaffold_closeout.py 2>&1 >/dev/null)"
missing_rc=$?
set -e
[[ "${missing_rc}" -ne 0 ]] || { echo "missing ISSUE_ID did not fail" >&2; exit 1; }
[[ "${missing_err}" == *"ISSUE_ID is required"* ]] || { echo "missing ISSUE_ID error message mismatch" >&2; exit 1; }

# 2) valid ISSUE_ID should create expected closeout file
create_out="$(ISSUE_ID="${TMP_ISSUE_ID}" ISSUE_TITLE="smoke title" OWNER="smoke-owner" python3 ops/bmad/scaffold_closeout.py)"
[[ "${create_out}" == "${TMP_CLOSEOUT_PATH}" ]] || { echo "unexpected scaffold output path" >&2; exit 1; }
[[ -f "${TMP_CLOSEOUT_PATH}" ]] || { echo "closeout file not created" >&2; exit 1; }

grep -q "Issue: #${TMP_ISSUE_ID}" "${TMP_CLOSEOUT_PATH}" || { echo "issue id not rendered" >&2; exit 1; }
grep -q "Title: smoke title" "${TMP_CLOSEOUT_PATH}" || { echo "title not rendered" >&2; exit 1; }
grep -q "Owner: smoke-owner" "${TMP_CLOSEOUT_PATH}" || { echo "owner not rendered" >&2; exit 1; }

# 3) second run without OVERWRITE should fail when file exists
set +e
exists_err="$(ISSUE_ID="${TMP_ISSUE_ID}" python3 ops/bmad/scaffold_closeout.py 2>&1 >/dev/null)"
exists_rc=$?
set -e
[[ "${exists_rc}" -ne 0 ]] || { echo "existing file check did not fail" >&2; exit 1; }
[[ "${exists_err}" == *"closeout already exists"* ]] || { echo "existing file error message mismatch" >&2; exit 1; }

echo "smoketest-bmad-closeout-scaffold: PASS"
