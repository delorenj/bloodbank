#!/usr/bin/env bash
#
# Bloodbank v3 Dapr subscribe smoke test.
#
# Publishes a CloudEvents envelope via the Dapr HTTP publish API and
# verifies that Dapr delivers it back to the echo-sub subscriber app.
# Exercises the full Dapr pubsub round-trip: HTTP publish -> daprd ->
# pubsub.jetstream -> NATS stream -> daprd consumer -> echo-sub callback.
#
# Companion to smoketest-dapr.sh (which exercises publish only). This is
# the first test that proves the DELIVERY path works; real services can
# plug in against the same contract.
#
# Usage:
#   bash ops/v3/smoketest/smoketest-dapr-subscribe.sh
#   bash ops/v3/smoketest/smoketest-dapr-subscribe.sh --correlation-id MY_ID
#
# Exit codes:
#   0 — PASS
#   1 — sandbox not reachable, publish failed, or delivery timeout
#   2 — envelope received but validation failed

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_PROJECT_NAME="bloodbank-v3"
COMPOSE_FILE="${BLOODBANK_ROOT}/compose/v3/docker-compose.yml"

# daprd-subscribe exposes its HTTP publish API on host 3501 by default.
DAPR_HTTP="${DAPR_HTTP:-http://127.0.0.1:3501}"

# echo-sub exposes its /inspect/* hook on host 3301 by default.
ECHO_SUB_HTTP="${ECHO_SUB_HTTP:-http://127.0.0.1:3301}"

PUBSUB_NAME="bloodbank-v3-pubsub"
TOPIC="event.dapr.subscribe.ping"

# How long to wait for Dapr to deliver the published event to echo-sub.
DELIVERY_TIMEOUT_SECS=10

CORRELATION_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --correlation-id)     CORRELATION_ID="${2:-}"; shift 2;;
    --correlation-id=*)   CORRELATION_ID="${1#*=}"; shift;;
    -h|--help)            sed -n '3,18p' "${BASH_SOURCE[0]}"; exit 0;;
    *)                    echo "smoketest-dapr-subscribe: unknown arg: $1" >&2; exit 1;;
  esac
done

if [[ -z "${CORRELATION_ID}" ]]; then
  CORRELATION_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)"
  EVENT_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)"
else
  EVENT_ID="${CORRELATION_ID}"
fi
EVENT_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "smoketest-dapr-subscribe: correlation_id=${CORRELATION_ID}"
echo "smoketest-dapr-subscribe: event_id=${EVENT_ID}"

fail() { local rc="$1"; shift; echo "smoketest-dapr-subscribe: FAIL -- $*" >&2; exit "${rc}"; }

# -----------------------------------------------------------------------------
# 1. Preconditions
# -----------------------------------------------------------------------------

if ! docker compose --project-name "${COMPOSE_PROJECT_NAME}" --profile dapr-subscribe -f "${COMPOSE_FILE}" ps daprd-subscribe --format json >/dev/null 2>&1; then
  fail 1 "daprd-subscribe not running; bring up with: docker compose --project-name ${COMPOSE_PROJECT_NAME} --profile dapr-subscribe -f compose/v3/docker-compose.yml up -d nats nats-init dapr-placement echo-sub daprd-subscribe"
fi

# Dapr healthz returns 204 when components are loaded.
if ! curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${DAPR_HTTP}/v1.0/healthz" | grep -q '^204$'; then
  fail 1 "daprd-subscribe healthz did not return 204 at ${DAPR_HTTP}/v1.0/healthz"
fi

# echo-sub healthz returns 204 when ready to receive.
if ! curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${ECHO_SUB_HTTP}/healthz" | grep -q '^204$'; then
  fail 1 "echo-sub healthz did not return 204 at ${ECHO_SUB_HTTP}/healthz"
fi

# -----------------------------------------------------------------------------
# 2. Clear echo-sub inspection buffer so prior runs don't pollute this test
# -----------------------------------------------------------------------------

curl -sS -X POST "${ECHO_SUB_HTTP}/inspect/reset" >/dev/null \
  || fail 1 "could not reset echo-sub inspect buffer"

# -----------------------------------------------------------------------------
# 3. Publish a CloudEvent via Dapr
# -----------------------------------------------------------------------------

ENVELOPE=$(cat <<JSON
{
  "specversion": "1.0",
  "id": "${EVENT_ID}",
  "source": "urn:33god:cli:dapr-subscribe-smoketest",
  "type": "dapr.subscribe.ping",
  "subject": "dapr-subscribe-smoketest/canonical",
  "time": "${EVENT_TIME}",
  "datacontenttype": "application/json",
  "dataschema": "urn:33god:holyfields:schema:dapr.subscribe.ping.v1",
  "correlationid": "${CORRELATION_ID}",
  "causationid": null,
  "producer": "dapr-subscribe-smoketest-cli",
  "service": "smoketest",
  "domain": "smoketest",
  "schemaref": "dapr.subscribe.ping.v1",
  "data": {"ping": true, "via": "dapr-subscribe"}
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

echo "smoketest-dapr-subscribe: published via Dapr to topic ${TOPIC} (HTTP 204)"

# -----------------------------------------------------------------------------
# 4. Poll echo-sub for the matching delivery
# -----------------------------------------------------------------------------
#
# Dapr delivery is async. Poll /inspect/received until the message with
# our event_id appears, or until we hit the timeout.

found=""
deadline=$(( $(date +%s) + DELIVERY_TIMEOUT_SECS ))

while [[ $(date +%s) -lt ${deadline} ]]; do
  # Ask echo-sub for its message buffer. `id == EVENT_ID` is our match.
  if curl -sS --max-time 3 "${ECHO_SUB_HTTP}/inspect/received" 2>/dev/null \
      | python3 -c "
import json, sys
want = sys.argv[1]
data = json.load(sys.stdin)
for m in data.get('messages', []):
    if isinstance(m, dict) and m.get('id') == want:
        sys.exit(0)
sys.exit(1)
" "${EVENT_ID}" ; then
    found="yes"
    break
  fi
  sleep 0.5
done

if [[ -z "${found}" ]]; then
  # Common cause of timeout: the same --correlation-id / event id was used
  # in a prior run within the JetStream dedup window (2 minutes by default
  # on BLOODBANK_V3_EVENTS). Dapr sets Nats-Msg-Id from the CloudEvent id,
  # so a duplicate publish is dropped at the broker. That is the
  # idempotency guarantee working correctly — it just makes the test
  # un-replayable against the same consumer within the dedup window.
  echo "smoketest-dapr-subscribe: timeout; echo-sub buffer dump:"
  curl -sS "${ECHO_SUB_HTTP}/inspect/received" | python3 -m json.tool >&2 || true
  fail 1 "echo-sub did not receive event id ${EVENT_ID} within ${DELIVERY_TIMEOUT_SECS}s (likely JetStream dedup: re-run with a different --correlation-id, or wait 2 minutes)"
fi

echo "smoketest-dapr-subscribe: delivery received at echo-sub (id=${EVENT_ID})"

# -----------------------------------------------------------------------------
# 5. Validate the delivered envelope shape
# -----------------------------------------------------------------------------
#
# Capture curl output to a variable first so the Python validator can
# receive it on stdin without colliding with its own heredoc-sourced
# script body. (Piping `curl | python3 - <<HEREDOC` empties stdin
# because the heredoc consumes it before the pipe data is readable.)

RECEIVED_JSON="$(curl -sS --max-time 3 "${ECHO_SUB_HTTP}/inspect/received" 2>/dev/null)"

python3 -c "
import json, sys
raw = sys.argv[1]
expected_id = sys.argv[2]
expected_corr = sys.argv[3]
expected_topic = sys.argv[4]
expected_pubsub = sys.argv[5]

try:
    data = json.loads(raw)
except json.JSONDecodeError as e:
    print(f'inspect/received is not JSON: {e}', file=sys.stderr)
    sys.exit(2)

# Find the message with our event_id (buffer may contain other messages).
env = None
for m in data.get('messages', []):
    if isinstance(m, dict) and m.get('id') == expected_id:
        env = m
        break
if env is None:
    print(f'delivered buffer did not contain id {expected_id}', file=sys.stderr)
    sys.exit(2)

problems = []
if env.get('specversion') != '1.0':
    problems.append(f\"specversion: expected 1.0, got {env.get('specversion')!r}\")
if env.get('type') != 'dapr.subscribe.ping':
    problems.append(f\"type: expected dapr.subscribe.ping, got {env.get('type')!r}\")
if env.get('correlationid') != expected_corr:
    problems.append(f\"correlationid: expected {expected_corr}, got {env.get('correlationid')!r}\")
if env.get('data', {}).get('ping') is not True:
    problems.append(f\"data.ping: expected true, got {env.get('data')!r}\")
if env.get('data', {}).get('via') != 'dapr-subscribe':
    problems.append(f\"data.via: expected dapr-subscribe, got {env.get('data', {}).get('via')!r}\")
# Dapr-added envelope fields (prove delivery came through Dapr, not direct-NATS).
if env.get('pubsubname') != expected_pubsub:
    problems.append(f\"pubsubname: expected {expected_pubsub} (Dapr adds this on delivery), got {env.get('pubsubname')!r}\")
if env.get('topic') != expected_topic:
    problems.append(f\"topic: expected {expected_topic} (Dapr adds this on delivery), got {env.get('topic')!r}\")
if problems:
    print('envelope validation failed:', file=sys.stderr)
    for p in problems:
        print(f'  - {p}', file=sys.stderr)
    sys.exit(2)
sys.exit(0)
" "${RECEIVED_JSON}" "${EVENT_ID}" "${CORRELATION_ID}" "${TOPIC}" "${PUBSUB_NAME}" \
  || fail 2 "envelope validation failed"

echo "smoketest-dapr-subscribe: PASS"
exit 0
