#!/usr/bin/env bash
#
# Schema ↔ validator consistency check.
#
# For every *.v1.json under bloodbank/schemas/bloodbank/v*/**, build a
# minimal envelope using the schema's declared (type, kind) and run it
# through core.validate.assert_contract. Any schema whose declared type
# is rejected by the stdlib contract surface (allowlist drift, banned-token
# slip, kind/action mismatch) flags as FAIL.
#
# This is the safety net against the "false confidence" drift the
# RECOMMENDATION.md fold-in was meant to eliminate: schemas live in this
# repo, the validator lives in this repo, and this smoke asserts they
# stay aligned without anyone having to remember to check.
#
# Stdlib-only. No Docker. No jsonschema required (only assert_contract).
#
# Exit codes:
#   0 — every schema's declared type passes the validator's contract checks
#   1 — at least one schema declares a type the validator rejects
#   2 — unexpected error (e.g. validator not importable)
#
# Usage:
#   bash ops/smoketest/smoketest-schema-contract-consistency.sh

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

cd "$BLOODBANK_ROOT"

PYTHONPATH="${BLOODBANK_ROOT}/services/agent-hooks${PYTHONPATH:+:$PYTHONPATH}" \
python3 - <<'PY'
import json
import sys
import uuid
from pathlib import Path

try:
    from core.validate import (
        ContractViolation,
        EVENT_ACTIONS,
        COMMAND_ACTIONS,
        assert_contract,
        subject_for,
    )
except Exception as exc:  # noqa: BLE001
    print(f"smoketest-schema-contract-consistency: cannot import validator: {exc!r}", file=sys.stderr)
    sys.exit(2)


ROOT = Path("schemas/bloodbank")
schemas = sorted(ROOT.rglob("*.v1.json"))
if not schemas:
    print("smoketest-schema-contract-consistency: no schemas found", file=sys.stderr)
    sys.exit(2)


def minimal_envelope(ce_type: str, kind: str) -> dict:
    """Build the smallest envelope that should pass assert_contract for (type, kind)."""
    domain = ce_type.split(".")[2]
    subject = subject_for(ce_type, kind)
    env = {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": "urn:33god:test:schema-consistency",
        "type": ce_type,
        "subject": subject,
        "time": "2026-01-01T00:00:00.000Z",
        "correlationid": str(uuid.uuid4()),
        "causationid": str(uuid.uuid4()),
        "producer": "schema-consistency-smoketest",
        "service": "schema-consistency-smoketest",
        "domain": domain,
        "kind": kind,
        "data": {},
        "actor": {"type": "service", "agent_id": "bloodbank.test.schema-consistency"},
    }
    if kind == "event":
        env["ordering_key"] = "test:1"
    elif kind == "command":
        env["command_id"] = str(uuid.uuid4())
        env["idempotency_key"] = "test:1"
        env["delivery"] = "single_consumer"
    return env


pass_count = 0
fail_count = 0

print(f"smoketest-schema-contract-consistency: verifying {len(schemas)} schema(s)")

for sp in schemas:
    rel = sp.relative_to(Path("schemas")).as_posix()
    try:
        s = json.loads(sp.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {rel}: cannot parse JSON: {exc}")
        fail_count += 1
        continue

    type_prop = s.get("properties", {}).get("type", {})
    kind_prop = s.get("properties", {}).get("kind", {})
    domain_prop = s.get("properties", {}).get("domain", {})

    if "const" not in type_prop:
        print(f"FAIL {rel}: properties.type.const missing")
        fail_count += 1
        continue
    if "const" not in kind_prop:
        print(f"FAIL {rel}: properties.kind.const missing")
        fail_count += 1
        continue
    if "const" not in domain_prop:
        print(f"FAIL {rel}: properties.domain.const missing")
        fail_count += 1
        continue

    ce_type = type_prop["const"]
    kind = kind_prop["const"]
    domain = domain_prop["const"]

    # type segment 3 must equal domain.const
    if ce_type.split(".")[2] != domain:
        print(f"FAIL {rel}: type segment 3 != domain.const ({ce_type.split('.')[2]!r} vs {domain!r})")
        fail_count += 1
        continue

    env = minimal_envelope(ce_type, kind)

    try:
        assert_contract(env)
    except ContractViolation as exc:
        action = ce_type.split(".")[-1]
        hint = ""
        if kind == "event" and action not in EVENT_ACTIONS:
            hint = f" (hint: action {action!r} missing from EVENT_ACTIONS allowlist in validate.py §8.1)"
        elif kind == "command" and action not in COMMAND_ACTIONS:
            hint = f" (hint: action {action!r} missing from COMMAND_ACTIONS allowlist in validate.py §8.2)"
        print(f"FAIL {ce_type}: {exc}{hint}")
        fail_count += 1
        continue

    print(f"PASS {ce_type}")
    pass_count += 1

print(f"smoketest-schema-contract-consistency: {pass_count} pass, {fail_count} fail")

if fail_count:
    print("smoketest-schema-contract-consistency: FAIL")
    sys.exit(1)

print("smoketest-schema-contract-consistency: PASS")
PY
