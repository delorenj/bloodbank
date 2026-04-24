#!/usr/bin/env bash
#
# Bloodbank v3 command + reply round-trip smoke test.
#
# Proves the BLOODBANK_V3_COMMANDS stream handles the command.*/reply.*
# subject topology defined in ADR-0001:
#
#   1. Publisher emits a command envelope on `command.smoketest.ping`.
#   2. A responder role consumes the command, constructs a reply, and
#      publishes it on `reply.smoketest.ping` with matching correlation_id
#      and `in_reply_to` pointing at the command_id.
#   3. The original publisher's role consumes the reply and validates the
#      correlation chain.
#
# This exercises:
#   - `command.>` subject landing in BLOODBANK_V3_COMMANDS
#   - `reply.>` subject landing in BLOODBANK_V3_COMMANDS
#   - Workqueue retention (ack removes the message)
#   - Correlation ID preservation across command → reply
#
# This is a NATS-direct test, parallel to smoketest.sh. Dapr wiring for
# commands (separate pubsub component bound to BLOODBANK_V3_COMMANDS) is
# a future piece; the transport-level contract has to be right first.
#
# Usage:
#   bash ops/v3/smoketest/smoketest-command.sh                       # fresh UUIDs
#   bash ops/v3/smoketest/smoketest-command.sh --correlation-id MY   # deterministic
#
# Exit codes:
#   0 — PASS
#   1 — sandbox/stream missing, publish failed, or fetch timeout
#   2 — envelope received but validation failed

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_PROJECT_NAME="bloodbank-v3"
COMPOSE_FILE="${BLOODBANK_ROOT}/compose/v3/docker-compose.yml"
STREAM="BLOODBANK_V3_COMMANDS"
COMMAND_SUBJECT="command.smoketest.ping"
REPLY_SUBJECT="reply.smoketest.ping"
RECEIVE_TIMEOUT="10s"

CORRELATION_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --correlation-id)     CORRELATION_ID="${2:-}"; shift 2;;
    --correlation-id=*)   CORRELATION_ID="${1#*=}"; shift;;
    -h|--help)            sed -n '3,24p' "${BASH_SOURCE[0]}"; exit 0;;
    *)                    echo "smoketest-command: unknown arg: $1" >&2; exit 1;;
  esac
done

# Consumer names are always fresh per-run (see smoketest.sh rationale).
CONSUMER_SUFFIX="$$-$(date +%s%N)"
CMD_CONSUMER="smoketest-cmd-in-${CONSUMER_SUFFIX}"
REPLY_CONSUMER="smoketest-reply-in-${CONSUMER_SUFFIX}"

if [[ -z "${CORRELATION_ID}" ]]; then
  CORRELATION_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)"
  COMMAND_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)"
else
  COMMAND_ID="${CORRELATION_ID}-cmd"
fi
REPLY_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)"
CMD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "smoketest-command: correlation_id=${CORRELATION_ID}"
echo "smoketest-command: command_id=${COMMAND_ID}"
echo "smoketest-command: reply_id=${REPLY_ID}"

nats_run() {
  docker run --rm --network bloodbank-v3-network natsio/nats-box:0.14.5 \
    nats -s nats://nats:4222 "$@"
}

nats_pipe() {
  docker run --rm -i --network bloodbank-v3-network natsio/nats-box:0.14.5 \
    nats -s nats://nats:4222 "$@"
}

cleanup() {
  nats_run consumer rm "${STREAM}" "${CMD_CONSUMER}"   --force >/dev/null 2>&1 || true
  nats_run consumer rm "${STREAM}" "${REPLY_CONSUMER}" --force >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() { local rc="$1"; shift; echo "smoketest-command: FAIL -- $*" >&2; exit "${rc}"; }

# -----------------------------------------------------------------------------
# 1. Preconditions: stream exists
# -----------------------------------------------------------------------------

if ! docker compose --project-name "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" ps nats --format json >/dev/null 2>&1; then
  fail 1 "sandbox not reachable; run 'docker compose --project-name ${COMPOSE_PROJECT_NAME} -f compose/v3/docker-compose.yml up -d nats nats-init'"
fi

if ! nats_run stream info "${STREAM}" >/dev/null 2>&1; then
  fail 1 "stream ${STREAM} missing; run nats-init"
fi

# -----------------------------------------------------------------------------
# 2. Create consumers BEFORE publishing.
# -----------------------------------------------------------------------------
# BLOODBANK_V3_COMMANDS is a workqueue stream — consumers must use
# `--deliver all` (workqueue semantics eat a message on ack, so "new" is
# ambiguous and NATS rejects it with error 10101). Workqueue + filtered
# consumers means any lingering messages on this run's subjects would be
# consumed by this run, but since we always consume exactly what we
# publish and the stream drains on ack, the queue stays clean across
# runs.

nats_run consumer add "${STREAM}" "${CMD_CONSUMER}" \
  --filter "${COMMAND_SUBJECT}" \
  --deliver all \
  --ack explicit \
  --replay instant \
  --pull \
  --defaults >/dev/null

nats_run consumer add "${STREAM}" "${REPLY_CONSUMER}" \
  --filter "${REPLY_SUBJECT}" \
  --deliver all \
  --ack explicit \
  --replay instant \
  --pull \
  --defaults >/dev/null

# -----------------------------------------------------------------------------
# 3. Publish the command
# -----------------------------------------------------------------------------

COMMAND=$(cat <<JSON
{
  "command_id": "${COMMAND_ID}",
  "correlationid": "${CORRELATION_ID}",
  "causationid": null,
  "type": "smoketest.ping",
  "time": "${CMD_TIME}",
  "issued_by": "smoketest-cli",
  "target_service": "smoketest-responder",
  "reply_to": "${REPLY_SUBJECT}",
  "timeout_ms": 5000,
  "payload_schema": "smoketest.ping.v1",
  "command_payload": {"ping": true}
}
JSON
)

printf '%s' "${COMMAND}" \
  | nats_pipe pub "${COMMAND_SUBJECT}" --force-stdin >/dev/null \
  || fail 1 "publish to ${COMMAND_SUBJECT} failed"

echo "smoketest-command: published command on ${COMMAND_SUBJECT}"

# -----------------------------------------------------------------------------
# 4. Responder role: consume command, build reply
# -----------------------------------------------------------------------------

RECEIVED_CMD="$(nats_run consumer next "${STREAM}" "${CMD_CONSUMER}" \
  --wait "${RECEIVE_TIMEOUT}" --ack --raw 2>/dev/null || true)"

if [[ -z "${RECEIVED_CMD}" ]]; then
  fail 1 "responder: receive timeout waiting for ${COMMAND_SUBJECT}"
fi

# Extract command_id + correlationid from the received command via Python
# (stdlib) so the reply actually correlates rather than trusting we got our
# own message back by chance.
RECEIVED_CMD_ID="$(python3 -c "
import json, sys
env = json.loads(sys.argv[1])
print(env['command_id'])
" "${RECEIVED_CMD}" 2>/dev/null || echo "")"
RECEIVED_CORR_ID="$(python3 -c "
import json, sys
env = json.loads(sys.argv[1])
print(env['correlationid'])
" "${RECEIVED_CMD}" 2>/dev/null || echo "")"

if [[ "${RECEIVED_CMD_ID}" != "${COMMAND_ID}" ]]; then
  fail 2 "responder received wrong command_id: expected ${COMMAND_ID}, got ${RECEIVED_CMD_ID}"
fi
if [[ "${RECEIVED_CORR_ID}" != "${CORRELATION_ID}" ]]; then
  fail 2 "responder received wrong correlationid: expected ${CORRELATION_ID}, got ${RECEIVED_CORR_ID}"
fi

echo "smoketest-command: responder consumed command (id=${RECEIVED_CMD_ID})"

# -----------------------------------------------------------------------------
# 5. Publish reply
# -----------------------------------------------------------------------------

REPLY_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
REPLY=$(cat <<JSON
{
  "reply_id": "${REPLY_ID}",
  "in_reply_to": "${RECEIVED_CMD_ID}",
  "correlationid": "${RECEIVED_CORR_ID}",
  "causationid": "${RECEIVED_CMD_ID}",
  "type": "smoketest.pong",
  "time": "${REPLY_TIME}",
  "status": "SUCCESS",
  "result": {"pong": true}
}
JSON
)

printf '%s' "${REPLY}" \
  | nats_pipe pub "${REPLY_SUBJECT}" --force-stdin >/dev/null \
  || fail 1 "publish to ${REPLY_SUBJECT} failed"

echo "smoketest-command: responder published reply on ${REPLY_SUBJECT}"

# -----------------------------------------------------------------------------
# 6. Caller role: consume reply, validate correlation chain
# -----------------------------------------------------------------------------

RECEIVED_REPLY="$(nats_run consumer next "${STREAM}" "${REPLY_CONSUMER}" \
  --wait "${RECEIVE_TIMEOUT}" --ack --raw 2>/dev/null || true)"

if [[ -z "${RECEIVED_REPLY}" ]]; then
  fail 1 "caller: receive timeout waiting for ${REPLY_SUBJECT}"
fi

python3 -c "
import json, sys
raw = sys.argv[1]
expected_cmd_id = sys.argv[2]
expected_corr = sys.argv[3]
expected_reply_id = sys.argv[4]

try:
    env = json.loads(raw)
except json.JSONDecodeError as e:
    print(f'reply is not JSON: {e}', file=sys.stderr); sys.exit(2)

problems = []
if env.get('reply_id') != expected_reply_id:
    problems.append(f\"reply_id: expected {expected_reply_id}, got {env.get('reply_id')!r}\")
if env.get('in_reply_to') != expected_cmd_id:
    problems.append(f\"in_reply_to: expected {expected_cmd_id}, got {env.get('in_reply_to')!r}\")
if env.get('correlationid') != expected_corr:
    problems.append(f\"correlationid: expected {expected_corr}, got {env.get('correlationid')!r}\")
if env.get('causationid') != expected_cmd_id:
    problems.append(f\"causationid: expected {expected_cmd_id}, got {env.get('causationid')!r}\")
if env.get('type') != 'smoketest.pong':
    problems.append(f\"type: expected smoketest.pong, got {env.get('type')!r}\")
if env.get('status') != 'SUCCESS':
    problems.append(f\"status: expected SUCCESS, got {env.get('status')!r}\")
if env.get('result', {}).get('pong') is not True:
    problems.append(f\"result.pong: expected true, got {env.get('result')!r}\")
if problems:
    print('reply envelope validation failed:', file=sys.stderr)
    for p in problems: print(f'  - {p}', file=sys.stderr)
    sys.exit(2)
sys.exit(0)
" "${RECEIVED_REPLY}" "${COMMAND_ID}" "${CORRELATION_ID}" "${REPLY_ID}" \
  || fail 2 "reply envelope validation failed"

echo "smoketest-command: PASS"
exit 0
