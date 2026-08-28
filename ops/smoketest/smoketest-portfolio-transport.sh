#!/usr/bin/env bash
# Publish one schema-valid portfolio root through the live Bloodbank JetStream,
# pull it back, and run the canonical consumer validator over the exact bytes.

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STREAM="BLOODBANK_EVENTS"
TYPE="bloodbank.portfolio.intake.received"
SUBJECT="bloodbank.evt.portfolio.intake.received"
CONSUMER_NAME="portfolio-smoketest-$$-$(date +%s%N)"
CORRELATION_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)"
EVENT_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)"
EVENT_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

nats_run() {
  docker run --rm \
    --network bloodbank-network \
    natsio/nats-box:0.14.5 \
    nats -s nats://nats:4222 "$@"
}

nats_pipe() {
  docker run --rm -i \
    --network bloodbank-network \
    natsio/nats-box:0.14.5 \
    nats -s nats://nats:4222 "$@"
}

cleanup() {
  nats_run consumer rm "${STREAM}" "${CONSUMER_NAME}" --force >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
  echo "smoketest-portfolio-transport: FAIL -- $*" >&2
  exit 1
}

# This proof intentionally does not start, initialize, or reconfigure services.
# A missing live binding remains an explicit deployment gate.
docker ps --filter 'name=^/bloodbank-nats$' --filter status=running \
  --format '{{.Names}}' 2>/dev/null | grep -qx bloodbank-nats \
  || fail "Bloodbank NATS is not already running (residual runtime gate)"
nats_run stream info "${STREAM}" >/dev/null 2>&1 \
  || fail "${STREAM} is not already provisioned (residual runtime gate)"

ENVELOPE="$(
  PYTHONPATH="${BLOODBANK_ROOT}/services/agent-hooks:${BLOODBANK_ROOT}/ops/smoketest" \
    python3 - "${CORRELATION_ID}" "${EVENT_ID}" "${EVENT_TIME}" <<'PY'
import json
import sys

from core.validate import validate_envelope
from portfolio_contract import build_envelope

correlation_id, event_id, occurred_at = sys.argv[1:]
envelope = build_envelope(
    "bloodbank.portfolio.intake.received",
    correlation_id=correlation_id,
    event_id=event_id,
    occurred_at=occurred_at,
)
envelope["source"] = "urn:33god:service:bloodbank-portfolio-smoketest"
envelope["producer"] = "bloodbank-portfolio-smoketest"
envelope["service"] = "bloodbank-portfolio-smoketest"
envelope["actor"] = {
    "type": "service",
    "agent_id": "bloodbank.test.portfolio-transport",
}
envelope["data"]["intake_id"] = f"transport-{event_id}"
envelope["data"]["idempotency_key"] = f"portfolio.intake:{event_id}:received"
envelope["idempotency_key"] = envelope["data"]["idempotency_key"]
envelope["data"]["request_summary"] = "Verify the Bloodbank portfolio transport binding."

# Publisher boundary: an invalid envelope never reaches transport.
validate_envelope(envelope)
print(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
PY
)"

nats_run consumer add "${STREAM}" "${CONSUMER_NAME}" \
  --filter "${SUBJECT}" \
  --deliver new \
  --ack explicit \
  --replay instant \
  --pull \
  --defaults >/dev/null

printf '%s' "${ENVELOPE}" | nats_pipe pub "${SUBJECT}" --force-stdin >/dev/null \
  || fail "publish failed"

RECEIVED_RAW="$(
  nats_run consumer next "${STREAM}" "${CONSUMER_NAME}" \
    --wait 10s --ack --raw 2>/dev/null || true
)"
[[ -n "${RECEIVED_RAW}" ]] || fail "receive timeout"
[[ "${RECEIVED_RAW}" == "${ENVELOPE}" ]] \
  || fail "JetStream did not preserve the published JSON bytes"

PYTHONPATH="${BLOODBANK_ROOT}/services/agent-hooks" \
python3 - "${ENVELOPE}" "${RECEIVED_RAW}" "${TYPE}" "${SUBJECT}" <<'PY' \
  || fail "consumer validation failed"
import json
import sys

from core.validate import validate_envelope

expected_raw, received_raw, expected_type, expected_subject = sys.argv[1:]
expected = json.loads(expected_raw)
received = json.loads(received_raw)

# Consumer boundary: full contract + JSON Schema validation runs after the
# actual JetStream delivery, then exact preservation is asserted.
validate_envelope(received)
if received != expected:
    raise SystemExit("JetStream did not preserve the published envelope")
if received["type"] != expected_type or received["subject"] != expected_subject:
    raise SystemExit("delivered envelope is bound to the wrong contract subject")
if received["correlationid"] != received["data"]["correlation_id"]:
    raise SystemExit("correlation lineage was not preserved")
if received["causationid"] is not None or received["data"]["causation_id"] is not None:
    raise SystemExit("root causation must remain null")
PY

echo "smoketest-portfolio-transport: PASS publisher -> JetStream -> validator"
