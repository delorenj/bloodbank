#!/usr/bin/env bash
# Deterministic smoke test for recovery_artifact_cleanup helper.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from pathlib import Path

import ops.bmad.recovery_artifact_cleanup as h

orig_list_branches = h._list_backup_branches
orig_list_bundles = h._list_bundle_paths
orig_delete_branch = h._delete_branch
orig_delete_file = h._delete_file
orig_bundle_age = h._bundle_age_hours

try:
    branches = [
        "backup/main-divergence-20260515T010000Z",
        "backup/main-divergence-20260515T020000Z",
        "backup/main-divergence-20260515T030000Z",
    ]
    bundles = [
        Path("/tmp/bloodbank-main-20260515T010000Z.bundle"),
        Path("/tmp/bloodbank-main-20260515T020000Z.bundle"),
    ]

    h._list_backup_branches = lambda _repo: list(branches)
    h._list_bundle_paths = lambda _bundle_dir: list(bundles)
    h._bundle_age_hours = lambda p, _now: 0.5 if p == bundles[0] else 48.0

    # Case 1: dry-run planning with age threshold (young bundle excluded from apply-set)
    payload = h.evaluate(
        Path("."),
        Path("/tmp"),
        keep_branches=1,
        keep_bundles=0,
        min_bundle_age_hours=1.0,
        apply=False,
        now_epoch=0,
    )
    assert payload["dry_run"] is True, payload
    assert payload["backup_branches_kept"] == [branches[-1]], payload
    assert payload["backup_branches_remove"] == branches[:-1], payload
    assert payload["bundle_files_remove"] == [str(bundles[0]), str(bundles[1])], payload
    assert payload["bundle_files_skip_too_young"] == [str(bundles[0])], payload
    assert payload["bundle_files_remove_age_eligible"] == [str(bundles[1])], payload

    # Case 2: apply execution obeys age threshold
    deleted_branches = []
    deleted_files = []

    h._delete_branch = lambda _repo, br: (deleted_branches.append(br) or True, "deleted")
    h._delete_file = lambda fp: (deleted_files.append(str(fp)) or True, "deleted")

    payload_apply = h.evaluate(
        Path("."),
        Path("/tmp"),
        keep_branches=1,
        keep_bundles=0,
        min_bundle_age_hours=1.0,
        apply=True,
        now_epoch=0,
    )
    assert payload_apply["dry_run"] is False, payload_apply
    assert sorted(deleted_branches) == sorted(branches[:-1]), deleted_branches
    assert deleted_files == [str(bundles[1])], deleted_files
    assert payload_apply["ok"] is True, payload_apply

finally:
    h._list_backup_branches = orig_list_branches
    h._list_bundle_paths = orig_list_bundles
    h._delete_branch = orig_delete_branch
    h._delete_file = orig_delete_file
    h._bundle_age_hours = orig_bundle_age
PY

echo "smoketest-bmad-recovery-artifact-cleanup: PASS"
