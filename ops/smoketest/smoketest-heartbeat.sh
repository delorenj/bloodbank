#!/usr/bin/env bash
#
# Bloodbank heartbeat integration smoke test — first real-world event.
#
# Unlike the other smoketests under this directory (which probe transport
# with ephemeral publishes), this one verifies the full first-real-event
# pipeline:
#
#   heartbeat-tick (long-running publisher)
#     → daprd-heartbeat (Dapr pub/sub through pubsub.jetstream)
#       → BLOODBANK_EVENTS stream on NATS JetStream
#         → daprd-heartbeat consumer (with --app-port to recorder)
#           → heartbeat-recorder (HTTP callback / records to memory)
#
# The test then queries heartbeat-recorder's /inspect/received endpoint
# and asserts:
#   - At least N ticks have arrived (default N=2)
#   - tick_seq is monotonic (no skips, no duplicates within one producer)
#   - All ticks share the same producer_id and started_at
#   - Each envelope is shape-valid CloudEvents 1.0 with the heartbeat data
#
# Preconditions (caller responsibility):
#   docker compose --project-name bloodbank --profile heartbeat \
#     -f compose/docker-compose.yml \
#     up -d nats nats-init dapr-placement heartbeat-recorder \
#           daprd-heartbeat heartbeat-tick
#
# Usage:
#   bash ops/smoketest/smoketest-heartbeat.sh
#   bash ops/smoketest/smoketest-heartbeat.sh --min-ticks 4 --wait 25
#
# Exit codes:
#   0 — PASS
#   1 — sandbox not reachable, recorder unhealthy, or tick count below min
#   2 — envelope validation failed

set -euo pipefail

RECORDER_HTTP="${RECORDER_HTTP:-http://127.0.0.1:3601}"
MIN_TICKS=2
WAIT_SECONDS=15

while [[ $# -gt 0 ]]; do
  case "$1" in
    --min-ticks)     MIN_TICKS="${2:-}"; shift 2;;
    --min-ticks=*)   MIN_TICKS="${1#*=}"; shift;;
    --wait)          WAIT_SECONDS="${2:-}"; shift 2;;
    --wait=*)        WAIT_SECONDS="${1#*=}"; shift;;
    -h|--help)       sed -n '3,28p' "${BASH_SOURCE[0]}"; exit 0;;
    *)               echo "smoketest-heartbeat: unknown arg: $1" >&2; exit 1;;
  esac
done

echo "smoketest-heartbeat: min_ticks=${MIN_TICKS} wait=${WAIT_SECONDS}s recorder=${RECORDER_HTTP}"

fail() { local rc="$1"; shift; echo "smoketest-heartbeat: FAIL -- $*" >&2; exit "${rc}"; }

# -----------------------------------------------------------------------------
# 1. Recorder must be reachable + healthy
# -----------------------------------------------------------------------------
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${RECORDER_HTTP}/healthz" 2>/dev/null || echo "000")
if [[ "${code}" != "204" ]]; then
  fail 1 "heartbeat-recorder healthz did not return 204 at ${RECORDER_HTTP}/healthz (got ${code})"
fi

# -----------------------------------------------------------------------------
# 2. Reset the buffer so this run isn't polluted by prior ticks
# -----------------------------------------------------------------------------
curl -sS -X POST "${RECORDER_HTTP}/inspect/reset" >/dev/null \
  || fail 1 "could not reset recorder buffer"

echo "smoketest-heartbeat: recorder buffer reset; waiting ${WAIT_SECONDS}s for ticks"

# -----------------------------------------------------------------------------
# 3. Wait for ticks to land. Poll every 2s, exit early if we hit min.
# -----------------------------------------------------------------------------
deadline=$(( $(date +%s) + WAIT_SECONDS ))
final_count=0
while [[ $(date +%s) -lt ${deadline} ]]; do
  count=$(curl -sS --max-time 3 "${RECORDER_HTTP}/inspect/received" 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['count'])" 2>/dev/null \
    || echo 0)
  if [[ "${count}" -ge "${MIN_TICKS}" ]]; then
    final_count="${count}"
    break
  fi
  sleep 2
done

if [[ "${final_count}" -lt "${MIN_TICKS}" ]]; then
  echo "smoketest-heartbeat: only ${final_count} ticks after ${WAIT_SECONDS}s (need ${MIN_TICKS})"
  echo "Recorder dump:"
  curl -sS "${RECORDER_HTTP}/inspect/received" | python3 -m json.tool >&2 || true
  fail 1 "tick count below threshold (${final_count} < ${MIN_TICKS})"
fi

echo "smoketest-heartbeat: ${final_count} ticks observed"

# -----------------------------------------------------------------------------
# 4. Validate shape + monotonic invariants
# -----------------------------------------------------------------------------
RECEIVED_JSON="$(curl -sS --max-time 3 "${RECORDER_HTTP}/inspect/received" 2>/dev/null)"

python3 -c "
import json, sys
data = json.loads(sys.argv[1])
min_ticks = int(sys.argv[2])

envelopes = data.get('messages', [])

problems = []

# 1. Envelope-level shape
for i, env in enumerate(envelopes):
    if not isinstance(env, dict):
        problems.append(f'envelope[{i}] is not an object')
        continue
    for required in ('specversion', 'id', 'source', 'type', 'time', 'correlationid', 'producer', 'service', 'domain', 'data'):
        if required not in env:
            problems.append(f'envelope[{i}] missing {required!r}')
    if env.get('specversion') != '1.0':
        problems.append(f\"envelope[{i}] specversion = {env.get('specversion')!r}, want '1.0'\")
    if env.get('type') != 'bloodbank.v1.system.heartbeat.received':
        problems.append(f\"envelope[{i}] type = {env.get('type')!r}, want 'bloodbank.v1.system.heartbeat.received'\")
    if env.get('kind') != 'event':
        problems.append(f\"envelope[{i}] kind = {env.get('kind')!r}, want 'event'\")
    if env.get('domain') != 'system':
        problems.append(f\"envelope[{i}] domain = {env.get('domain')!r}, want 'system'\")
    d = env.get('data', {})
    for required in ('tick_seq', 'producer_id', 'started_at'):
        if required not in d:
            problems.append(f'envelope[{i}].data missing {required!r}')

# 2. All envelopes come from exactly one producer and process lifetime.
producer_ids = {env.get('data', {}).get('producer_id') for env in envelopes}
started_ats = {env.get('data', {}).get('started_at') for env in envelopes}
if len(producer_ids) != 1:
    problems.append(f'expected exactly 1 producer_id, got {producer_ids}')
if len(started_ats) != 1:
    problems.append(f'expected exactly 1 started_at value, got {started_ats}')

# 3. Tick sequence is monotonic with no gaps or duplicates.
tick_seqs = [env.get('data', {}).get('tick_seq') for env in envelopes]
if not all(isinstance(seq, int) for seq in tick_seqs):
    problems.append(f'non-integer tick_seq values: {tick_seqs}')
elif tick_seqs:
    expected = list(range(tick_seqs[0], tick_seqs[-1] + 1))
    if tick_seqs != expected:
        problems.append(f'tick_seq values are not contiguous and monotonic: {tick_seqs}')

if problems:
    print('heartbeat smoke validation failed:', file=sys.stderr)
    for p in problems:
        print(f'  - {p}', file=sys.stderr)
    sys.exit(2)
print(f'OK: {len(envelopes)} envelopes, {len(producer_ids)} producer(s), all monotonic')
sys.exit(0)
" "${RECEIVED_JSON}" "${MIN_TICKS}" \
  || fail 2 "envelope validation failed"

echo "smoketest-heartbeat: PASS"
exit 0
