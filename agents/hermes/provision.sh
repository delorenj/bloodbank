#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERMES_HOME="${REPO_ROOT}/agents/hermes/runtime"
HERMES_BIN="${HERMES_BIN:-/home/delorenj/code/hermes-agent/.venv/bin/hermes}"

mkdir -p "$HERMES_HOME"

# Bootstrap Hermes home/state if missing
env HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" status >/dev/null

# Repo-scoped defaults for Bloodbank Hermes
env HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" config set terminal.cwd "$REPO_ROOT"
env HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" config set model.default qwen3.6
env HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" config set model.provider ollama-launch

echo "Provisioned Hermes runtime at: $HERMES_HOME"
env HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" doctor
