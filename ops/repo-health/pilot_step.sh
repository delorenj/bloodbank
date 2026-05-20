#!/usr/bin/env bash
set -euo pipefail

# Hermes pilot loop helper:
# - evaluate idle gate from repo-health snapshot
# - always run strict cleanliness gate
# - on strict failure, optionally attempt submodule-drift auto-heal once
# - run full artifact+cleanup only when gate requires it

INTERVAL_MINUTES="${INTERVAL_MINUTES:-60}"
KEEP="${KEEP:-8}"
REPORT="${REPORT:-1}"
DRIFT_AUTOHEAL_ON_STRICT_FAIL="${DRIFT_AUTOHEAL_ON_STRICT_FAIL:-1}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
DECISION_OUT="_bmad_output/evidence/repo-health-idle-decision-${TS}.json"

TMP_SNAPSHOT=""
if [[ -n "${SNAPSHOT_PATH:-}" ]]; then
  SNAPSHOT_PATH="${SNAPSHOT_PATH}"
else
  TMP_SNAPSHOT="$(mktemp)"
  SNAPSHOT_PATH="$TMP_SNAPSHOT"
  python3 cli/bb.py repo-health --json --out "$SNAPSHOT_PATH" >/dev/null
fi

cleanup() {
  if [[ -n "$TMP_SNAPSHOT" && -f "$TMP_SNAPSHOT" ]]; then
    rm -f "$TMP_SNAPSHOT"
  fi
}
trap cleanup EXIT

mkdir -p "$(dirname "$DECISION_OUT")"
python3 ops/repo-health/idle_gate.py \
  --snapshot "$SNAPSHOT_PATH" \
  --interval-minutes "$INTERVAL_MINUTES" > "$DECISION_OUT"

echo "$DECISION_OUT"

# Keep idle-decision artifacts bounded similarly to repo-health snapshots.
python3 - "$KEEP" <<'PY'
from pathlib import Path
import sys

keep = int(sys.argv[1])
paths = sorted(Path("_bmad_output/evidence").glob("repo-health-idle-decision-*.json"))
if keep <= 0:
    doomed = paths
elif keep >= len(paths):
    doomed = []
else:
    doomed = paths[: len(paths) - keep]
for p in doomed:
    p.unlink(missing_ok=True)
PY

SHOULD_CAPTURE_FULL="$({ python3 - "$DECISION_OUT" <<'PY'
import json, sys
with open(sys.argv[1], 'r', encoding='utf-8') as f:
    d = json.load(f)
print('1' if d.get('should_capture_full') else '0')
PY
} )"

run_strict_with_autoheal() {
  if mise run repo-health:strict; then
    return 0
  fi

  if [[ "$DRIFT_AUTOHEAL_ON_STRICT_FAIL" != "1" ]]; then
    echo "repo-health:strict failed (auto-heal disabled)"
    return 1
  fi

  echo "repo-health:strict failed; attempting submodule drift auto-heal"
  if ! python3 ops/bmad/reconcile_submodule_gitlink_drift.py --repo . --apply; then
    echo "repo-health:strict auto-heal attempt failed"
    return 1
  fi

  echo "repo-health:strict drift auto-heal applied; retrying strict gate"
  mise run repo-health:strict
}

# Always run strict gate for repo safety and liveness evidence in logs.
run_strict_with_autoheal

if [[ "$SHOULD_CAPTURE_FULL" == "1" ]]; then
  mise run repo-health:artifact
  KEEP="$KEEP" REPORT="$REPORT" mise run repo-health:cleanup
else
  echo "repo-health-idle-throttle: skipped full artifact capture"
fi
