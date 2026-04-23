#!/usr/bin/env bash
#
# Bloodbank v3 canonical smoke test.
#
# Publishes a canonical CloudEvents envelope to event.smoketest.ping on
# NATS JetStream, receives it via a short-lived pull consumer, and
# validates the round-trip. See ops/v3/smoketest/README.md for scope.
#
# Usage:
#   bash ops/v3/smoketest/smoketest.sh                            # fresh UUID per run
#   bash ops/v3/smoketest/smoketest.sh --correlation-id my-id     # deterministic
#
# Exit codes:
#   0 — PASS; published event was received with matching id.
#   1 — sandbox not reachable, stream missing, or receive timeout.
#   2 — event received but envelope validation failed.

set -euo pipefail

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Resolve bloodbank repo root from this script's location.
BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_PROJECT_NAME="bloodbank-v3"
COMPOSE_FILE="${BLOODBANK_ROOT}/compose/v3/docker-compose.yml"
STREAM="BLOODBANK_V3_EVENTS"
SUBJECT="event.smoketest.ping"
CANONICAL_EVENT_TEMPLATE="${BLOODBANK_ROOT}/ops/v3/smoketest/canonical-event.json"
RECEIVE_TIMEOUT="10s"

CORRELATION_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --correlation-id)
      CORRELATION_ID="${2:-}"
      shift 2
      ;;
    --correlation-id=*)
      CORRELATION_ID="${1#*=}"
      shift
      ;;
    -h|--help)
      sed -n '3,17p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "smoketest: unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

# Fresh IDs unless a deterministic correlation id was provided.
if [[ -z "${CORRELATION_ID}" ]]; then
  CORRELATION_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)"
  EVENT_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen)"
else
  # Deterministic mode: correlation_id + event_id are stable so downstream
  # systems can dedup. Consumer name stays fresh (see below).
  EVENT_ID="${CORRELATION_ID}"
fi
# Consumer name is ALWAYS fresh per run. Idempotency lives in the
# correlation_id / event_id; the test's consumer bookkeeping is an
# implementation detail. Re-using a consumer name across runs caused
# stale-delivery-state races; a fresh per-run name is boring and correct.
CONSUMER_NAME="smoketest-$$-$(date +%s%N)"

EVENT_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "smoketest: correlation_id=${CORRELATION_ID}"
echo "smoketest: event_id=${EVENT_ID}"
echo "smoketest: consumer=${CONSUMER_NAME}"

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

# Run a nats-box command against the sandbox network.
nats_run() {
  docker run --rm \
    --network "bloodbank-v3-network" \
    natsio/nats-box:0.14.5 \
    nats -s nats://nats:4222 "$@"
}

# Run nats-box with stdin piped to the container.
nats_pipe() {
  docker run --rm -i \
    --network "bloodbank-v3-network" \
    natsio/nats-box:0.14.5 \
    nats -s nats://nats:4222 "$@"
}

cleanup() {
  # Always try to drop the throwaway consumer.
  nats_run consumer rm "${STREAM}" "${CONSUMER_NAME}" --force >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
  local rc="$1"
  shift
  echo "smoketest: FAIL -- $*" >&2
  exit "${rc}"
}

# -----------------------------------------------------------------------------
# 1. Ensure sandbox is up and streams exist
# -----------------------------------------------------------------------------

if ! docker compose --project-name "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" ps nats --format json >/dev/null 2>&1; then
  fail 1 "docker compose project '${COMPOSE_PROJECT_NAME}' not reachable; run 'docker compose up -d nats nats-init'"
fi

if ! nats_run stream info "${STREAM}" >/dev/null 2>&1; then
  echo "smoketest: stream ${STREAM} missing; running nats-init"
  docker compose --project-name "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" run --rm nats-init >/dev/null 2>&1 \
    || fail 1 "nats-init did not leave ${STREAM} in place"
fi

# -----------------------------------------------------------------------------
# 2. Build the canonical envelope
# -----------------------------------------------------------------------------

if [[ ! -f "${CANONICAL_EVENT_TEMPLATE}" ]]; then
  fail 1 "missing template ${CANONICAL_EVENT_TEMPLATE}"
fi

# Substitute placeholders; keep the rest verbatim.
ENVELOPE="$(
  sed \
    -e "s|__ID__|${EVENT_ID}|" \
    -e "s|__CORRELATION_ID__|${CORRELATION_ID}|" \
    -e "s|__TIME__|${EVENT_TIME}|" \
    "${CANONICAL_EVENT_TEMPLATE}"
)"

# Sanity: it must still be valid JSON.
if ! printf '%s' "${ENVELOPE}" | python3 -m json.tool >/dev/null 2>&1; then
  fail 2 "envelope is not valid JSON after substitution"
fi

# -----------------------------------------------------------------------------
# 3. Create a short-lived pull consumer for this run
# -----------------------------------------------------------------------------

# If the consumer already exists (deterministic mode re-run), remove it first
# so we get a clean delivery cursor for this run.
nats_run consumer rm "${STREAM}" "${CONSUMER_NAME}" --force >/dev/null 2>&1 || true

# --deliver new: only messages published AFTER this consumer is created
# (we publish on the next step). This keeps the smoke test clean of
# messages left behind by prior runs in long-lived sandboxes.
nats_run consumer add "${STREAM}" "${CONSUMER_NAME}" \
  --filter "${SUBJECT}" \
  --deliver new \
  --ack explicit \
  --replay instant \
  --pull \
  --defaults >/dev/null

# -----------------------------------------------------------------------------
# 4. Publish
# -----------------------------------------------------------------------------

printf '%s' "${ENVELOPE}" \
  | nats_pipe pub "${SUBJECT}" --force-stdin >/dev/null \
  || fail 1 "publish to ${SUBJECT} failed"

echo "smoketest: published to ${SUBJECT}"

# -----------------------------------------------------------------------------
# 5. Consume
# -----------------------------------------------------------------------------

# `consumer next` returns the next pending message and acks it with --ack.
# --raw prints payload only (no headers/metadata framing) so we can parse JSON.
# Capture stdout only so nats CLI diagnostics on stderr don't leak into
# the validator's JSON parse attempt.
RECEIVED_RAW="$(nats_run consumer next "${STREAM}" "${CONSUMER_NAME}" \
  --wait "${RECEIVE_TIMEOUT}" \
  --ack \
  --raw 2>/dev/null || true)"

if [[ -z "${RECEIVED_RAW}" ]]; then
  fail 1 "receive timeout after ${RECEIVE_TIMEOUT}"
fi

# -----------------------------------------------------------------------------
# 6. Validate
# -----------------------------------------------------------------------------

# Extract known fields via python (stdlib json); avoids jq dependency on host.
python3 - "${RECEIVED_RAW}" "${EVENT_ID}" "${CORRELATION_ID}" <<'PY' || fail 2 "envelope validation failed"
import json, sys
raw, expected_id, expected_correlation = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    env = json.loads(raw)
except json.JSONDecodeError as e:
    print(f"received payload is not JSON: {e}", file=sys.stderr)
    sys.exit(2)
problems = []
if env.get("specversion") != "1.0":
    problems.append(f"specversion: expected 1.0, got {env.get('specversion')!r}")
if env.get("type") != "smoketest.ping":
    problems.append(f"type: expected smoketest.ping, got {env.get('type')!r}")
if env.get("id") != expected_id:
    problems.append(f"id: expected {expected_id}, got {env.get('id')!r}")
if env.get("correlationid") != expected_correlation:
    problems.append(f"correlationid: expected {expected_correlation}, got {env.get('correlationid')!r}")
if env.get("data", {}).get("ping") is not True:
    problems.append(f"data.ping: expected true, got {env.get('data')!r}")
if problems:
    print("envelope validation failed:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    sys.exit(2)
sys.exit(0)
PY

echo "smoketest: PASS"
exit 0
