#!/usr/bin/env bash
set -euo pipefail

# Hermes pilot loop helper:
# - evaluate idle gate from repo-health snapshot
# - always run strict cleanliness gate
# - run full artifact+cleanup only when gate requires it

INTERVAL_MINUTES="${INTERVAL_MINUTES:-60}"
KEEP="${KEEP:-8}"
REPORT="${REPORT:-1}"

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

# Always run strict gate for repo safety and liveness evidence in logs.
mise run repo-health:strict

if [[ "$SHOULD_CAPTURE_FULL" == "1" ]]; then
  mise run repo-health:artifact
  KEEP="$KEEP" REPORT="$REPORT" mise run repo-health:cleanup
else
  echo "repo-health-idle-throttle: skipped full artifact capture"
fi
