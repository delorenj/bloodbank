#!/usr/bin/env bash
#
# agent-hooks deployed-config health check (standalone).
#
# Validates that EVERY hook config this service deploys (claude/codex/copilot +
# the hermes fleet) produces error-free hooks:
#   - our bloodbank publisher commands build a contract+schema-valid envelope
#     (no NATS, no side effects);
#   - foreign commands (hindsight, git-checkpoint, lint-skills, …) are static-
#     checked only (referenced scripts exist + executable + `bash -n`);
#   - hermes publisher commands are present + allowlisted.
#
# Exit 0 when every deployed config is clean; 1 when any has a failing hook.
#
# NOTE: NOT part of `smoketest:schemas` — it inspects LIVE machine config state
# (incl. foreign hook scripts), so it is environment-dependent by design.
#
# Usage: bash ops/smoketest/smoketest-hook-health.sh   (or `mise run health:hooks:check`)

set -euo pipefail
BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec python3 "${BLOODBANK_ROOT}/services/agent-hooks/health/hook_healthcheck.py" --check
