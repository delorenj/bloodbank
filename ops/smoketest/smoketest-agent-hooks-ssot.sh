#!/usr/bin/env bash
#
# agent-hooks SSOT propagation verifier.
#
# Asserts that hooks.master.json (the single source of truth) is fully
# propagated and contract-clean:
#
#   1. `sync.py --check` is green — SSOT, lock, and every generated artifact
#      (per-agent hooks config + event_map.generated.json) are in sync, and
#      there are no unresolved ambiguities.
#   2. For EVERY agent binding, the resolved (ce_type, ordering_bucket) builds
#      a CloudEvents envelope that passes the v1 contract AND validates against
#      its bloodbank/schemas JSON Schema (data shaped from the schema's
#      required fields). Reuses each publisher's real actor/source identity.
#
# Stdlib + jsonschema (already a dev dep of validate.py). No NATS/Docker.
#
# Exit codes: 0 PASS · 1 a binding failed · 2 sync drift/ambiguity.
#
# Usage: bash ops/smoketest/smoketest-agent-hooks-ssot.sh

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_DIR="${BLOODBANK_ROOT}/services/agent-hooks"

echo "agent-hooks-ssot: 1/2 sync --check"
if ! python3 "${SERVICE_DIR}/sync.py" --check; then
  echo "agent-hooks-ssot: FAIL — SSOT/artifacts out of sync or unresolved ambiguity (run 'mise run hooks:sync')" >&2
  exit 2
fi

echo "agent-hooks-ssot: 2/2 per-binding envelope contract + schema validation"
python3 - "$SERVICE_DIR" <<'PY'
import sys
from pathlib import Path

service_dir = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(service_dir))

import sync  # SSOT loader + effective_type
from core.envelope import build_envelope
from core.validate import load_schema_for

import claude.publish as cl
import copilot.publish as cp
import codex.publish as cx
import hermes.publish as hm

IDENT = {
    "claude": dict(source=cl.CLAUDE_SOURCE, producer=cl.CLAUDE_PRODUCER, service=cl.CLAUDE_SERVICE, actor=cl.CLAUDE_ACTOR),
    "copilot": dict(source=cp.COPILOT_SOURCE, producer=cp.COPILOT_PRODUCER, service=cp.COPILOT_SERVICE, actor=cp.COPILOT_ACTOR),
    "codex": dict(source=cx.CODEX_SOURCE, producer=cx.CODEX_PRODUCER, service=cx.CODEX_SERVICE, actor=cx.CODEX_ACTOR),
    "hermes": dict(source=hm.HERMES_SOURCE, producer=hm.HERMES_PRODUCER, service=hm.HERMES_SERVICE, actor=hm.HERMES_ACTOR),
}

UUID = "00000000-0000-0000-0000-000000000001"


def _sample(spec):
    if not isinstance(spec, dict):
        return "x"
    if "enum" in spec and spec["enum"]:
        return spec["enum"][0]
    t = spec.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        t = non_null[0] if non_null else t[0]
    return {"string": "x", "integer": 1, "number": 1, "boolean": True, "array": [], "object": {}}.get(t, "x")


def _data_for(ce_type):
    schema = load_schema_for(ce_type)
    data_schema = schema.get("properties", {}).get("data", {})
    props = data_schema.get("properties", {})
    out = {}
    for key in data_schema.get("required", []):
        out[key] = _sample(props.get(key, {}))
    return out


master = sync.load_master()
lock = sync.load_lock()
lifecycle = master["lifecycle"]

fails = 0
checked = 0
for agent_name, agent in master["agents"].items():
    if agent.get("dialect") in ("watcher", "runtime"):
        continue
    ident = IDENT[agent_name]
    for b in agent.get("bindings", []):
        ce_type, bucket = sync.effective_type(b, lifecycle, lock)
        try:
            data = _data_for(ce_type)
            build_envelope(
                ce_type=ce_type,
                kind=lifecycle.get(b.get("lifecycle", ""), {}).get("kind", "event"),
                source=ident["source"],
                producer=ident["producer"],
                service=ident["service"],
                actor=ident["actor"],
                data=data,
                correlation_id=UUID,
                causation_id=UUID,
                ordering_key=f"{bucket}:smoketest",
                validate=True,
            )
            checked += 1
            print(f"  PASS [{agent_name}] {b['native']:<20} -> {ce_type}")
        except Exception as exc:  # noqa: BLE001
            fails += 1
            print(f"  FAIL [{agent_name}] {b['native']:<20} -> {ce_type}: {exc!r}")

print(f"agent-hooks-ssot: {checked} binding(s) validated, {fails} failure(s)")
sys.exit(1 if fails else 0)
PY

echo "agent-hooks-ssot: PASS"
