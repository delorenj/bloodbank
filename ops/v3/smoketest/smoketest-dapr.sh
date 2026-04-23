#!/usr/bin/env bash
#
# Bloodbank v3 Dapr smoke test.
#
# Publishes a CloudEvents envelope via the Dapr HTTP publish API and
# receives it back through NATS JetStream via a throwaway pull consumer.
# This exercises the `bloodbank-v3-pubsub` Dapr component end-to-end:
#
#   curl POST → daprd → pubsub.jetstream → NATS subject event.* → stream
#
# See ops/v3/smoketest/README.md for scope. This is the Dapr-level
# companion to smoketest.sh (the pre-Dapr NATS-direct test).
#
# Usage:
#   bash ops/v3/smoketest/smoketest-dapr.sh                         # fresh id
#   bash ops/v3/smoketest/smoketest-dapr.sh --correlation-id MY_ID  # deterministic
#
# Exit codes:
#   0 — PASS
#   1 — sandbox/daprd not reachable, or fetch timeout
#   2 — envelope received but validation failed

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_PROJECT_NAME="bloodbank-v3"
COMPOSE_FILE="${BLOODBANK_ROOT}/compose/v3/docker-compose.yml"
STREAM="BLOODBANK_V3_EVENTS"
# Dapr HTTP API is exposed on the host port (default 3500).
DAPR_HTTP="${DAPR_HTTP:-http://127.0.0.1:3500}"
PUBSUB_NAME="bloodbank-v3-pubsub"
TOPIC="event.dapr.smoketest.ping"
RECEIVE_TIMEOUT="10s"

CORRELATION_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --correlation-id)       CORRELATION_ID="${2:-}"; shift 2;;
    --correlation-id=*)     CORRELATION_ID="${1#*=}"; shift;;
    -h|--help)              sed -n '3,17p' "${BASH_SOURCE[0]}"; exit 0;;
    *)                      echo "smoketest-dapr: unknown argument: $1" >&2; exit 1;;
  esac
done

if [[ -z "${CORRELATION_ID}" ]]; then
  CORRELATION_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)"
  EVENT_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)"
else
  EVENT_ID="${CORRELATION_ID}"
fi
# Consumer name is ALWAYS fresh per run. Idempotency lives in the
# correlation_id / event_id (which downstream systems dedup on), not in
# the test's consumer bookkeeping. Re-using a consumer name across runs
# caused stale-delivery-state races; a fresh per-run name is boring and
# correct.
CONSUMER_NAME="dapr-smoketest-$$-$(date +%s%N)"
EVENT_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "smoketest-dapr: correlation_id=${CORRELATION_ID}"
echo "smoketest-dapr: event_id=${EVENT_ID}"
echo "smoketest-dapr: consumer=${CONSUMER_NAME}"

nats_run() {
  docker run --rm --network bloodbank-v3-network natsio/nats-box:0.14.5 \
    nats -s nats://nats:4222 "$@"
}

cleanup() {
  nats_run consumer rm "${STREAM}" "${CONSUMER_NAME}" --force >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() { local rc="$1"; shift; echo "smoketest-dapr: FAIL -- $*" >&2; exit "${rc}"; }

# -----------------------------------------------------------------------------
# 1. Preconditions: sandbox + daprd healthy
# -----------------------------------------------------------------------------

if ! docker compose --project-name "${COMPOSE_PROJECT_NAME}" --profile dapr-smoketest -f "${COMPOSE_FILE}" ps daprd-smoketest --format json >/dev/null 2>&1; then
  fail 1 "daprd-smoketest not running; bring up with: docker compose --project-name ${COMPOSE_PROJECT_NAME} --profile dapr-smoketest -f compose/v3/docker-compose.yml up -d"
fi

# Dapr healthz returns 204 when components are loaded and API is ready.
if ! curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${DAPR_HTTP}/v1.0/healthz" | grep -q '^204$'; then
  fail 1 "dapr healthz did not return 204 at ${DAPR_HTTP}/v1.0/healthz"
fi

# NATS stream must exist (nats-init should have created it).
if ! nats_run stream info "${STREAM}" >/dev/null 2>&1; then
  fail 1 "stream ${STREAM} missing; run nats-init"
fi

# -----------------------------------------------------------------------------
# 2. Create a pull consumer BEFORE publishing so we have deliver-new semantics
# -----------------------------------------------------------------------------

nats_run consumer rm "${STREAM}" "${CONSUMER_NAME}" --force >/dev/null 2>&1 || true
nats_run consumer add "${STREAM}" "${CONSUMER_NAME}" \
  --filter "${TOPIC}" \
  --deliver new \
  --ack explicit \
  --replay instant \
  --pull \
  --defaults >/dev/null

# -----------------------------------------------------------------------------
# 3. Publish via Dapr HTTP API
# -----------------------------------------------------------------------------
#
# Per ADR-0001 topic-to-subject mapping, the Dapr topic name IS the NATS
# subject. Publishing to topic "event.dapr.smoketest.ping" lands on NATS
# subject "event.dapr.smoketest.ping" which matches the BLOODBANK_V3_EVENTS
# stream's `event.>` binding.

ENVELOPE=$(cat <<JSON
{
  "specversion": "1.0",
  "id": "${EVENT_ID}",
  "source": "urn:33god:cli:dapr-smoketest",
  "type": "dapr.smoketest.ping",
  "subject": "dapr-smoketest/canonical",
  "time": "${EVENT_TIME}",
  "datacontenttype": "application/json",
  "dataschema": "urn:33god:holyfields:schema:dapr.smoketest.ping.v1",
  "correlationid": "${CORRELATION_ID}",
  "causationid": null,
  "producer": "dapr-smoketest-cli",
  "service": "smoketest",
  "domain": "smoketest",
  "schemaref": "dapr.smoketest.ping.v1",
  "data": {"ping": true, "via": "dapr"}
}
JSON
)

HTTP_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' \
  --max-time 10 \
  -H 'Content-Type: application/cloudevents+json' \
  -d "${ENVELOPE}" \
  "${DAPR_HTTP}/v1.0/publish/${PUBSUB_NAME}/${TOPIC}")"

if [[ "${HTTP_STATUS}" != "204" ]]; then
  fail 1 "dapr publish returned HTTP ${HTTP_STATUS} (expected 204)"
fi

echo "smoketest-dapr: published via Dapr to topic ${TOPIC} (HTTP 204)"

# -----------------------------------------------------------------------------
# 4. Fetch from NATS consumer
# -----------------------------------------------------------------------------

# Capture stdout only so nats CLI diagnostics on stderr don't leak into
# the validator's JSON parse attempt.
RECEIVED_RAW="$(nats_run consumer next "${STREAM}" "${CONSUMER_NAME}" \
  --wait "${RECEIVE_TIMEOUT}" --ack --raw 2>/dev/null || true)"

if [[ -z "${RECEIVED_RAW}" ]]; then
  fail 1 "receive timeout after ${RECEIVE_TIMEOUT} (consumer got no message)"
fi

# -----------------------------------------------------------------------------
# 5. Validate — Dapr adds envelope fields (topic, pubsubname, traceid, etc.);
#    we validate the fields we sent round-trip correctly.
# -----------------------------------------------------------------------------

python3 - "${RECEIVED_RAW}" "${EVENT_ID}" "${CORRELATION_ID}" "${TOPIC}" "${PUBSUB_NAME}" <<'PY' || fail 2 "envelope validation failed"
import json, sys
raw, expected_id, expected_correlation, expected_topic, expected_pubsub = sys.argv[1:6]
try:
    env = json.loads(raw)
except json.JSONDecodeError as e:
    print(f"received payload is not JSON: {e}", file=sys.stderr); sys.exit(2)

problems = []
if env.get("specversion") != "1.0":
    problems.append(f"specversion: expected 1.0, got {env.get('specversion')!r}")
if env.get("type") != "dapr.smoketest.ping":
    problems.append(f"type: expected dapr.smoketest.ping, got {env.get('type')!r}")
if env.get("id") != expected_id:
    problems.append(f"id: expected {expected_id}, got {env.get('id')!r}")
if env.get("correlationid") != expected_correlation:
    problems.append(f"correlationid: expected {expected_correlation}, got {env.get('correlationid')!r}")
if env.get("data", {}).get("ping") is not True:
    problems.append(f"data.ping: expected true, got {env.get('data')!r}")
if env.get("data", {}).get("via") != "dapr":
    problems.append(f"data.via: expected 'dapr', got {env.get('data', {}).get('via')!r}")
# Dapr-added fields that prove the route was Dapr, not direct-NATS.
if env.get("pubsubname") != expected_pubsub:
    problems.append(f"pubsubname: expected {expected_pubsub} (Dapr adds this), got {env.get('pubsubname')!r}")
if env.get("topic") != expected_topic:
    problems.append(f"topic: expected {expected_topic} (Dapr adds this), got {env.get('topic')!r}")
if problems:
    print("envelope validation failed:", file=sys.stderr)
    for p in problems: print(f"  - {p}", file=sys.stderr)
    sys.exit(2)
sys.exit(0)
PY

echo "smoketest-dapr: PASS"
exit 0
