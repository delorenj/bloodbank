#!/usr/bin/env bash
# Deterministic smoke test for pilot-step strict-fail submodule-drift auto-heal path.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

mkdir -p "$TMPDIR/ops/repo-health" "$TMPDIR/ops/bmad" "$TMPDIR/_bmad_output/evidence" "$TMPDIR/fakebin"
cp ops/repo-health/pilot_step.sh "$TMPDIR/ops/repo-health/pilot_step.sh"
cp ops/repo-health/idle_gate.py "$TMPDIR/ops/repo-health/idle_gate.py"
chmod +x "$TMPDIR/ops/repo-health/pilot_step.sh"

cat > "$TMPDIR/ops/bmad/reconcile_submodule_gitlink_drift.py" <<'PY'
#!/usr/bin/env python3
from pathlib import Path
Path('.autoheal-marker').write_text('applied\n', encoding='utf-8')
print('{"ok": true, "applied": true}')
PY
chmod +x "$TMPDIR/ops/bmad/reconcile_submodule_gitlink_drift.py"

cat > "$TMPDIR/snapshot.json" <<'JSON'
{
  "git_status": "## main...origin/main",
  "worktree_dirty": false,
  "issues_open": [],
  "prs_open": []
}
JSON

cat > "$TMPDIR/fakebin/mise" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
state_file=".strict-call-count"
count=0
if [[ -f "$state_file" ]]; then
  count="$(cat "$state_file")"
fi

if [[ "$1" != "run" ]]; then
  echo "unexpected mise invocation: $*" >&2
  exit 2
fi

case "$2" in
  repo-health:strict)
    count=$((count + 1))
    echo "$count" > "$state_file"
    if [[ "$count" -eq 1 ]]; then
      echo "simulated strict fail" >&2
      exit 1
    fi
    echo "simulated strict pass"
    exit 0
    ;;
  repo-health:artifact)
    : > .artifact-ran
    exit 0
    ;;
  repo-health:cleanup)
    : > .cleanup-ran
    exit 0
    ;;
  *)
    echo "unexpected task: $2" >&2
    exit 2
    ;;
esac
SH
chmod +x "$TMPDIR/fakebin/mise"

(
  cd "$TMPDIR"
  PATH="$TMPDIR/fakebin:$PATH" \
  SNAPSHOT_PATH="$TMPDIR/snapshot.json" \
  INTERVAL_MINUTES=60 \
  KEEP=2 \
  REPORT=1 \
  DRIFT_AUTOHEAL_ON_STRICT_FAIL=1 \
  bash ops/repo-health/pilot_step.sh > "$TMPDIR/out.log" 2>&1
)

grep -q "repo-health:strict failed; attempting submodule drift auto-heal" "$TMPDIR/out.log"
grep -q "repo-health:strict drift auto-heal applied; retrying strict gate" "$TMPDIR/out.log"
test -f "$TMPDIR/.autoheal-marker"

test "$(cat "$TMPDIR/.strict-call-count")" = "2"

echo "smoketest-bmad-repo-health-pilot-step-autoheal: PASS"
