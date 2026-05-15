#!/usr/bin/env bash
# Guard against risky inline gh body composition in BMAD operator docs/scripts.

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${BLOODBANK_ROOT}"

# Inline --body with quoted markdown is fragile in shell automation loops.
pattern='gh (issue create|pr create|pr edit|issue edit).*(--body(=| )")'

if rg -n "$pattern" ops/bmad >/tmp/bmad-gh-body-safety-hits.txt; then
  echo "Unsafe inline gh --body pattern detected:" >&2
  cat /tmp/bmad-gh-body-safety-hits.txt >&2
  exit 1
fi

echo "smoketest-bmad-github-body-safety: PASS"
