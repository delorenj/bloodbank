#!/usr/bin/env bash
#
# check-platform.sh -- Bloodbank v3 scaffold presence check.
#
# Static file-presence validator for the v3 scaffold. Pure bash + filesystem
# stat; no Docker, no Dapr, no NATS, no Apicurio, no network access.
#
# Exit codes:
#   0 -- every required file is present.
#   1 -- at least one required file is missing.
#
# See ops/v3/bootstrap/README.md for the wider contract and the sibling
# checker cli/v3/bb_v3.py (subcommand "doctor") which covers the same files
# with a slightly richer severity model.

set -euo pipefail

# Resolve the bloodbank repo root from this script's location so the checker
# works regardless of the current working directory.
BLOODBANK_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)

# List of required scaffold artifacts, as paths relative to BLOODBANK_ROOT.
# Keep this aligned with SCAFFOLD_MANIFEST in cli/v3/bb_v3.py.
REQUIRED_FILES=(
  "compose/v3/docker-compose.yml"
  "compose/v3/components/pubsub.yaml"
  "compose/v3/components/statestore.yaml"
  "compose/v3/components/secretstore.yaml"
  "compose/v3/nats/streams.json"
  "compose/v3/README.md"
  "ops/v3/bootstrap/check-platform.sh"
  "cli/v3/bb_v3.py"
)

total=${#REQUIRED_FILES[@]}
missing=0
present=0

for rel_path in "${REQUIRED_FILES[@]}"; do
  abs_path="${BLOODBANK_ROOT}/${rel_path}"
  if [[ -f "${abs_path}" ]]; then
    printf 'PASS %s\n' "${rel_path}"
    present=$((present + 1))
  else
    printf 'FAIL %s: missing or not a regular file\n' "${rel_path}"
    missing=$((missing + 1))
  fi
done

printf 'Bootstrap check: %d/%d artifacts present\n' "${present}" "${total}"

if (( missing > 0 )); then
  exit 1
fi

exit 0
