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
DRIFT_AUTOHEAL_PRECHECK="${DRIFT_AUTOHEAL_PRECHECK:-1}"

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

capture_runtime_drift_context() {
  local ts out
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  out="_bmad_output/evidence/runtime-drift-context-${ts}.json"

  python3 - "$out" <<'PY'
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
runtime = Path("agents/hermes/pm/runtime")


def run(cmd: list[str], cwd: Path | None = None) -> dict[str, object]:
    cp = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, capture_output=True)
    return {
        "cmd": " ".join(cmd),
        "code": cp.returncode,
        "stdout": cp.stdout.strip(),
        "stderr": cp.stderr.strip(),
    }

payload: dict[str, object] = {
    "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "runtime_path": str(runtime),
    "runtime_exists": runtime.exists(),
}

if runtime.exists():
    payload["runtime_probe"] = {
        "head": run(["git", "rev-parse", "HEAD"], cwd=runtime),
        "status": run(["git", "status", "--short", "--branch"], cwd=runtime),
        "last_commit": run(["git", "log", "-1", "--pretty=format:%H %cI %an %s"], cwd=runtime),
        "recent_reflog": run(["git", "reflog", "-10", "--date=iso"], cwd=runtime),
    }

payload["superproject_probe"] = {
    "submodule_status": run(["git", "submodule", "status", "--recursive"]),
    "git_status": run(["git", "status", "--short", "--branch"]),
}

out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
print(out)
PY
}

run_precheck_autoheal() {
  if [[ "$DRIFT_AUTOHEAL_PRECHECK" != "1" ]]; then
    return 0
  fi

  local drift_count
  drift_count="$({ python3 ops/bmad/reconcile_submodule_gitlink_drift.py --repo . | python3 -c 'import json,sys; print(json.load(sys.stdin).get("drift_count", 0))'; } 2>/dev/null || echo 0)"

  if [[ "$drift_count" == "0" ]]; then
    echo "repo-health:precheck no submodule drift detected"
    return 0
  fi

  echo "repo-health:precheck drift detected (${drift_count}); capturing context"
  capture_runtime_drift_context

  echo "repo-health:precheck drift detected (${drift_count}); attempting auto-heal"
  if python3 ops/bmad/reconcile_submodule_gitlink_drift.py --repo . --apply >/dev/null; then
    echo "repo-health:precheck drift auto-heal complete"
    return 0
  fi

  echo "repo-health:precheck auto-heal skipped (non-main branch or non-drift edits present)"
  return 0
}

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

# Opportunistically reconcile known drift pattern before strict gate.
run_precheck_autoheal

# Always run strict gate for repo safety and liveness evidence in logs.
run_strict_with_autoheal

if [[ "$SHOULD_CAPTURE_FULL" == "1" ]]; then
  mise run repo-health:artifact
  KEEP="$KEEP" REPORT="$REPORT" mise run repo-health:cleanup
else
  echo "repo-health-idle-throttle: skipped full artifact capture"
fi
