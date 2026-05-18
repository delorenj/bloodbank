#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

mkdir -p "$TMPDIR/evidence"

cat > "$TMPDIR/idle.json" <<'JSON'
{
  "git_status": "## main...origin/main",
  "worktree_dirty": false,
  "issues_open": [],
  "prs_open": []
}
JSON

cat > "$TMPDIR/busy.json" <<'JSON'
{
  "git_status": "## main...origin/main",
  "worktree_dirty": false,
  "issues_open": [{"number": 188}],
  "prs_open": []
}
JSON

# create synthetic artifact names for timestamp parsing
printf '{}\n' > "$TMPDIR/evidence/repo-health-20260518T220000Z.json"
printf '{}\n' > "$TMPDIR/evidence/repo-health-20260518T225000Z.json"

# idle + interval not elapsed => throttled
OUT1="$(python3 ops/repo-health/idle_gate.py --snapshot "$TMPDIR/idle.json" --evidence-dir "$TMPDIR/evidence" --interval-minutes 60 --now-utc 20260518T230000Z)"

echo "$OUT1" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["idle_state"] is True; assert d["should_capture_full"] is False; assert d["reason"]=="idle-throttled"'

# idle + interval elapsed => capture
OUT2="$(python3 ops/repo-health/idle_gate.py --snapshot "$TMPDIR/idle.json" --evidence-dir "$TMPDIR/evidence" --interval-minutes 60 --now-utc 20260519T000100Z)"

echo "$OUT2" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["idle_state"] is True; assert d["should_capture_full"] is True; assert d["reason"]=="idle-interval-elapsed"'

# non-idle => capture
OUT3="$(python3 ops/repo-health/idle_gate.py --snapshot "$TMPDIR/busy.json" --evidence-dir "$TMPDIR/evidence" --interval-minutes 60 --now-utc 20260518T230000Z)"

echo "$OUT3" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["idle_state"] is False; assert d["should_capture_full"] is True; assert d["reason"]=="non-idle-state"'

echo "smoketest-bmad-repo-health-idle-gate: PASS"
