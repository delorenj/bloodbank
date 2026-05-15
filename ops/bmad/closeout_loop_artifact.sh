#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -lt 1 ]]; then
  echo "usage: $0 <pr-number-or-url> [closeout-loop args...]" >&2
  exit 1
fi

ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="_bmad_output/evidence/closeout/closeout-loop-${ts}.json"
mkdir -p "$(dirname "$out")"

python3 ops/bmad/closeout_loop.py "$@" --out "$out"
echo "$out"
