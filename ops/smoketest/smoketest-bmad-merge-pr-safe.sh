#!/usr/bin/env bash
# Smoke test for ops/bmad/merge_pr_safe.py
# Covers machine-readable JSON output and merge/cleanup contract fields
# using an already merged PR reference (non-destructive path).

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${BLOODBANK_ROOT}"

MERGED_PR="${MERGED_PR:-117}"

merge_json="$(python3 ops/bmad/merge_pr_safe.py "${MERGED_PR}")"

python3 - <<'PY' "${merge_json}"
import json
import sys

payload = json.loads(sys.argv[1])
for key in [
    "state",
    "mergedAt",
    "merge_command_exit",
    "merge_command_stderr",
    "head_branch",
    "cleanup",
]:
    assert key in payload, payload

assert payload["state"] == "MERGED", payload
assert payload["mergedAt"], payload
assert isinstance(payload["merge_command_exit"], int), payload
assert isinstance(payload["merge_command_stderr"], str), payload

if payload["head_branch"] is not None:
    assert isinstance(payload["head_branch"], str), payload

cleanup = payload["cleanup"]
assert isinstance(cleanup, dict), payload
for key in [
    "local_branch_status",
    "local_branch_deleted",
    "linked_worktrees",
    "followup_commands",
]:
    assert key in cleanup, payload
assert cleanup["local_branch_status"] in {"not_applicable", "already_absent", "deleted", "failed"}, payload
assert isinstance(cleanup["linked_worktrees"], list), payload
assert isinstance(cleanup["followup_commands"], list), payload
if cleanup["local_branch_status"] in {"already_absent", "deleted"}:
    assert cleanup["local_branch_deleted"] is True, payload
if cleanup["local_branch_status"] == "failed":
    assert cleanup["local_branch_deleted"] is False, payload
PY

echo "smoketest-bmad-merge-pr-safe: PASS"
