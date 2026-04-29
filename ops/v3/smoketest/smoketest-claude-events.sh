#!/usr/bin/env bash
#
# Bloodbank v3 claude-events round-trip smoke test.
#
# Verifies that synthetic agent.* envelopes published through the
# claude-events daprd sidecar land on the recorder for all six event
# types we currently emit. Smoke gate for V3-108 (profile + recorder)
# and V3-109 (additional hook events).
#
# Pipeline under test:
#
#   curl POST CloudEvents envelope
#     → daprd-claude-events sidecar (host:3503)
#       → BLOODBANK_V3_EVENTS stream on NATS JetStream
#         → daprd-claude-events consumer (--app-port to recorder)
#           → claude-events-recorder
#               → /inspect/recorded
#
# Assertions:
#   - All six event types appear in count_by_type with count >= 1
#   - Per-session aggregate shows full lifecycle for the synthetic
#     session (started, ended, prompts_submitted >= 1, tool_requests >= 1,
#     tool_invocations >= 1, subagents_completed >= 1)
#   - Each envelope is shape-valid CloudEvents 1.0 with the expected `type`
#
# Preconditions (caller responsibility):
#   docker compose --project-name bloodbank-v3 --profile claude-events \
#     -f compose/v3/docker-compose.yml \
#     up -d nats nats-init dapr-placement claude-events-recorder daprd-claude-events
#
# Optional env:
#   SESSION_ID  override the synthetic session_id (default: random uuid).
#               useful for the human verification runlist where the
#               operator wants a pre-known id to filter on.
#
# Usage:
#   bash ops/v3/smoketest/smoketest-claude-events.sh
#   SESSION_ID=verify-synth-1 bash ops/v3/smoketest/smoketest-claude-events.sh
#
# Exit codes:
#   0 — PASS
#   1 — sandbox not reachable, recorder unhealthy, or events missing
#   2 — envelope validation failed

set -euo pipefail

DAPR_HTTP="${DAPR_HTTP:-http://127.0.0.1:3503}"
RECORDER_HTTP="${RECORDER_HTTP:-http://127.0.0.1:3602}"
PUBSUB="${PUBSUB:-bloodbank-v3-pubsub}"
WAIT_SECONDS=15

fail() { local rc="$1"; shift; echo "smoketest-claude-events: FAIL -- $*" >&2; exit "${rc}"; }

# -----------------------------------------------------------------------------
# 1. Sidecar + recorder must be reachable
# -----------------------------------------------------------------------------
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${DAPR_HTTP}/v1.0/healthz" 2>/dev/null || echo "000")
if [[ "${code}" != "204" ]]; then
  fail 1 "daprd-claude-events not ready at ${DAPR_HTTP} (got ${code})"
fi

code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${RECORDER_HTTP}/healthz" 2>/dev/null || echo "000")
if [[ "${code}" != "204" ]]; then
  fail 1 "claude-events-recorder not healthy at ${RECORDER_HTTP} (got ${code})"
fi

# -----------------------------------------------------------------------------
# 2. Reset the buffer
# -----------------------------------------------------------------------------
curl -sS -X POST "${RECORDER_HTTP}/inspect/reset" >/dev/null \
  || fail 1 "could not reset recorder buffer"

# -----------------------------------------------------------------------------
# 3. Publish synthetic envelopes (one of each agent.* type, same session_id)
# -----------------------------------------------------------------------------
# SESSION_ID is the synthetic session's id and the correlationid for every
# envelope in this run. Allows the runlist to filter by `data.session_id`.
SESSION_ID="${SESSION_ID:-$(python3 -c 'import uuid; print(uuid.uuid4())')}"
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "smoketest-claude-events: session_id=${SESSION_ID}"

publish_envelope() {
  local ev_type="$1" topic="$2" data="$3"
  local envelope
  envelope=$(python3 -c '
import json, sys, uuid
ev_type, session_id, now, data_json = sys.argv[1:]
print(json.dumps({
    "specversion": "1.0",
    "id": str(uuid.uuid4()),
    "source": "urn:33god:smoketest:claude-events",
    "type": ev_type,
    "subject": f"agent/{session_id}",
    "time": now,
    "datacontenttype": "application/json",
    "correlationid": session_id,
    "causationid": None,
    "producer": "claude-code",
    "service": "claude-code",
    "domain": "agent",
    "schemaref": ev_type + ".v1",
    "traceparent": "00-00000000000000000000000000000000-0000000000000000-00",
    "data": json.loads(data_json),
}))
' "$ev_type" "$SESSION_ID" "$NOW" "$data")

  local code
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
    -X POST \
    -H "Content-Type: application/cloudevents+json" \
    -d "$envelope" \
    "${DAPR_HTTP}/v1.0/publish/${PUBSUB}/${topic}" 2>/dev/null || echo "000")

  if [[ ! "$code" =~ ^2[0-9][0-9]$ ]]; then
    fail 1 "publish failed type=${ev_type} http=${code}"
  fi
}

# Order matches a real session lifecycle: start, prompt, request, invoke,
# subagent finishes, end. Recorder doesn't rely on order; ordering here is
# for human readability of the test trace.
publish_envelope "agent.session.started" "event.agent.session.started" \
  "{\"session_id\":\"${SESSION_ID}\",\"working_directory\":\"/tmp/smoketest\",\"git_branch\":\"main\",\"git_remote\":\"\",\"started_at\":\"${NOW}\"}"

publish_envelope "agent.prompt.submitted" "event.agent.prompt.submitted" \
  "{\"session_id\":\"${SESSION_ID}\",\"prompt_text\":\"list .claude/hooks\",\"prompt_length\":18,\"working_directory\":\"/tmp/smoketest\",\"git_branch\":\"main\"}"

publish_envelope "agent.tool.requested" "event.agent.tool.requested" \
  "{\"session_id\":\"${SESSION_ID}\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"ls .claude/hooks\"},\"working_directory\":\"/tmp/smoketest\",\"git_branch\":\"main\",\"turn_number\":1}"

publish_envelope "agent.tool.invoked" "event.agent.tool.invoked" \
  "{\"session_id\":\"${SESSION_ID}\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"ls .claude/hooks\"},\"working_directory\":\"/tmp/smoketest\",\"git_branch\":\"main\",\"git_status\":\"clean\",\"turn_number\":1,\"success\":true}"

publish_envelope "agent.subagent.completed" "event.agent.subagent.completed" \
  "{\"session_id\":\"${SESSION_ID}\",\"agent_type\":\"general-purpose\",\"stop_reason\":\"completed\",\"working_directory\":\"/tmp/smoketest\"}"

publish_envelope "agent.session.ended" "event.agent.session.ended" \
  "{\"session_id\":\"${SESSION_ID}\",\"end_reason\":\"user_stop\",\"duration_seconds\":42,\"total_turns\":1,\"tools_used\":{\"Bash\":1},\"files_modified\":[],\"git_commits\":[],\"final_status\":\"success\",\"working_directory\":\"/tmp/smoketest\",\"git_branch\":\"main\"}"

# -----------------------------------------------------------------------------
# 4. Wait for delivery
# -----------------------------------------------------------------------------
deadline=$(( $(date +%s) + WAIT_SECONDS ))
final_count=0
while [[ $(date +%s) -lt ${deadline} ]]; do
  count=$(curl -sS --max-time 3 "${RECORDER_HTTP}/inspect/recorded" 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['count'])" 2>/dev/null \
    || echo 0)
  if [[ "${count}" -ge 6 ]]; then
    final_count="${count}"
    break
  fi
  sleep 1
done

if [[ "${final_count}" -lt 6 ]]; then
  echo "Recorder dump:"
  curl -sS "${RECORDER_HTTP}/inspect/recorded" | python3 -m json.tool >&2 || true
  fail 1 "expected 6 envelopes, got ${final_count} after ${WAIT_SECONDS}s"
fi

# -----------------------------------------------------------------------------
# 5. Validate shape + per-session aggregate
# -----------------------------------------------------------------------------
RECEIVED_JSON="$(curl -sS --max-time 3 "${RECORDER_HTTP}/inspect/recorded" 2>/dev/null)"

python3 -c "
import json, sys
data = json.loads(sys.argv[1])
session_id = sys.argv[2]

problems = []
envelopes = data.get('envelopes', [])
count_by_type = data.get('count_by_type', {})
sessions = data.get('sessions', [])

# 1. count_by_type covers all six
expected_types = (
    'agent.session.started',
    'agent.session.ended',
    'agent.tool.invoked',
    'agent.prompt.submitted',
    'agent.tool.requested',
    'agent.subagent.completed',
)
for t in expected_types:
    if count_by_type.get(t, 0) < 1:
        problems.append(f'count_by_type missing or zero for {t}')

# 2. Per-envelope shape (CloudEvents 1.0 + 33GOD extensions)
for i, env in enumerate(envelopes):
    for required in ('specversion', 'id', 'source', 'type', 'time', 'correlationid', 'producer', 'service', 'domain', 'data'):
        if required not in env:
            problems.append(f'envelope[{i}] missing {required!r}')
    if env.get('specversion') != '1.0':
        problems.append(f\"envelope[{i}] specversion = {env.get('specversion')!r}\")
    if env.get('domain') != 'agent':
        problems.append(f\"envelope[{i}] domain = {env.get('domain')!r}\")

# 3. Per-session aggregate covers full new shape
matching = [s for s in sessions if s.get('session_id') == session_id]
if len(matching) != 1:
    problems.append(f'expected exactly one session entry for {session_id}, got {len(matching)}')
else:
    s = matching[0]
    for required_truthy in ('started', 'ended'):
        if not s.get(required_truthy):
            problems.append(f'session aggregate {required_truthy} should be true: {s}')
    for required_count in ('prompts_submitted', 'tool_requests', 'tool_invocations', 'subagents_completed'):
        if s.get(required_count, 0) < 1:
            problems.append(f'session aggregate {required_count} should be >= 1: {s}')
    if 'last_seen' not in s:
        problems.append(f'session aggregate missing last_seen: {s}')

if problems:
    print('claude-events smoke validation failed:', file=sys.stderr)
    for p in problems:
        print(f'  - {p}', file=sys.stderr)
    sys.exit(2)

print(f'OK: {len(envelopes)} envelopes across {len(count_by_type)} types, session aggregate complete')
sys.exit(0)
" "${RECEIVED_JSON}" "${SESSION_ID}" \
  || fail 2 "envelope validation failed"

echo "smoketest-claude-events: PASS"
exit 0
