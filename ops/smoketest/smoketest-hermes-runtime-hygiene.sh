#!/usr/bin/env bash
# Verify Hermes runtime hygiene: runtime state ignored, skeleton tracked.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

ignored=(
  "agents/hermes/runtime/.env"
  "agents/hermes/runtime/auth.json"
  "agents/hermes/runtime/auth.lock"
  "agents/hermes/runtime/config.yaml"
  "agents/hermes/runtime/logs/session.log"
)

for p in "${ignored[@]}"; do
  if ! git check-ignore -q "$p"; then
    echo "expected ignored path not ignored: $p" >&2
    exit 1
  fi
done

if git check-ignore -q "agents/hermes/runtime/.gitignore"; then
  echo "runtime/.gitignore must remain trackable" >&2
  exit 1
fi

if git check-ignore -q "agents/hermes/README.md"; then
  echo "agents/hermes/README.md should be tracked, not ignored" >&2
  exit 1
fi

pm_runtime_ignore="$(git config -f .gitmodules --get submodule.agents/hermes/pm/runtime.ignore || true)"
if [[ "$pm_runtime_ignore" != "all" ]]; then
  echo "agents/hermes/pm/runtime submodule must set ignore=all in .gitmodules" >&2
  exit 1
fi

echo "smoketest-hermes-runtime-hygiene: PASS"
