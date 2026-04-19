#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"

pass() {
  printf '[PASS] %s\n' "$1"
}

fail() {
  printf '[FAIL] %s\n' "$1" >&2
}

action() {
  printf '[ACTION] %s\n' "$1"
}

required_files=(
  "v3-implementation-plan.md"
  "cli/v3/README.md"
  "cli/v3/bb_v3.py"
  "ops/v3/bootstrap/README.md"
  "ops/v3/bootstrap/check-platform.sh"
  "compose/v3/README.md"
  "compose/v3/nats/README.md"
  "compose/v3/apicurio/README.md"
  "compose/v3/eventcatalog/README.md"
)

missing=0

pass "checking Bloodbank v3 scaffold from ${ROOT_DIR}"

for relative in "${required_files[@]}"; do
  if [[ -e "${ROOT_DIR}/${relative}" ]]; then
    pass "${relative} exists"
  else
    fail "${relative} is missing"
    action "Add ${relative} before wiring any runtime publish path."
    missing=1
  fi
done

if grep -Eq 'doctor|trace|replay|emit' "${ROOT_DIR}/cli/v3/bb_v3.py"; then
  pass "cli/v3/bb_v3.py declares the doctor, trace, replay, and emit stubs"
else
  fail "cli/v3/bb_v3.py does not expose the expected v3 command stubs"
  action "Add the doctor, trace, replay, and emit command skeletons to cli/v3/bb_v3.py."
  missing=1
fi

if grep -Eq 'Holyfields|Bloodbank owns runtime|does not publish real traffic' "${ROOT_DIR}/cli/v3/README.md"; then
  pass "cli/v3/README.md explains the Holyfields/Bloodbank ownership split"
else
  fail "cli/v3/README.md does not explain the ownership boundary clearly enough"
  action "Document that Holyfields owns schemas and Bloodbank owns runtime and ops."
  missing=1
fi

if grep -Eq 'no production side effects|No Docker, Dapr, NATS, or network activity' "${ROOT_DIR}/ops/v3/bootstrap/README.md"; then
  pass "ops/v3/bootstrap/README.md documents the local-only bootstrap posture"
else
  fail "ops/v3/bootstrap/README.md is missing the local-only bootstrap guidance"
  action "Document that bootstrap checks are static and do not require platform services."
  missing=1
fi

if [[ "${missing}" -ne 0 ]]; then
  action "Fix the missing scaffold files and rerun this check."
  exit 1
fi

pass "Bloodbank v3 bootstrap scaffold is present and locally verifiable."
action "Proceed to compileall and then wire runtime behavior in later tickets."
