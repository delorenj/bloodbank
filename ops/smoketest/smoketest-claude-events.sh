#!/usr/bin/env bash
#
# Bloodbank claude-events round-trip smoke test (v1 contract).
#
# Verifies that synthetic Claude-CLI events published through the
# claude-events daprd sidecar land on the recorder for the six v1 event
# types the hook now emits.
#
# Pipeline under test:
#
#   curl POST CloudEvents envelope (v1 contract)
#     → daprd-claude-events sidecar (host:3503)
#       → BLOODBANK_EVENTS stream on NATS JetStream
#         → daprd-claude-events consumer (--app-port to recorder)
#           → claude-events-recorder
#               → /inspect/recorded
#
# Event types under test (per bloodbank/docs/event-naming.md §15):
#   bloodbank.v1.cli.session.started
#   bloodbank.v1.cli.session.ended
#   bloodbank.v1.conversation.turn.started
#   bloodbank.v1.tool.tool_call.requested
#   bloodbank.v1.tool.tool_call.invoked
#   bloodbank.v1.agent.invocation.completed
#
# Assertions:
#   - All six event types appear in count_by_type with count >= 1
#   - Per-session aggregate covers the lifecycle (session_started, session_ended,
#     prompts_submitted >= 1, tool_requests >= 1, tool_invocations >= 1,
#     subagents_completed >= 1)
#   - Each envelope is a valid v1 contract envelope (kind, actor, ordering_key)
#
# Preconditions:
#   docker compose --project-name bloodbank --profile claude-events \
#     -f compose/docker-compose.yml \
#     up -d nats nats-init dapr-placement claude-events-recorder daprd-claude-events
#
# Usage:
#   bash ops/smoketest/smoketest-claude-events.sh
#   SESSION_ID=verify-synth-1 bash ops/smoketest/smoketest-claude-events.sh
#
# Exit codes:
#   0 — PASS
#   1 — sandbox not reachable, recorder unhealthy, or events missing
#   2 — envelope validation failed

set -euo pipefail

DAPR_HTTP="${DAPR_HTTP:-http://127.0.0.1:3503}"
RECORDER_HTTP="${RECORDER_HTTP:-http://127.0.0.1:3602}"
PUBSUB="${PUBSUB:-bloodbank-pubsub}"
WAIT_SECONDS=15

fail() { local rc="$1"; shift; echo "smoketest-claude-events: FAIL -- $*" >&2; exit "${rc}"; }

# Preconditions
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${DAPR_HTTP}/v1.0/healthz" 2>/dev/null || echo "000")
if [[ "${code}" != "204" ]]; then
  fail 1 "daprd-claude-events not ready at ${DAPR_HTTP} (got ${code})"
fi

code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${RECORDER_HTTP}/healthz" 2>/dev/null || echo "000")
if [[ "${code}" != "204" ]]; then
  fail 1 "claude-events-recorder not healthy at ${RECORDER_HTTP} (got ${code})"
fi

curl -sS -X POST "${RECORDER_HTTP}/inspect/reset" >/dev/null \
  || fail 1 "could not reset recorder buffer"

SESSION_ID="${SESSION_ID:-$(python3 -c 'import uuid; print(uuid.uuid4())')}"
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "smoketest-claude-events: session_id=${SESSION_ID}"

# publish_envelope ce_type subject ordering_key data_json
# Builds a v1 envelope (kind, actor, ordering_key) and POSTs to Dapr.
publish_envelope() {
  local ev_type="$1" topic="$2" ordering_key="$3" data="$4"
  local envelope
  envelope=$(python3 -c '
import json, sys, uuid
ce_type, subject, session_id, now, ordering_key, data_json = sys.argv[1:]
print(json.dumps({
    "specversion": "1.0",
    "id": str(uuid.uuid4()),
    "source": "urn:33god:smoketest:claude-events",
    "type": ce_type,
    "subject": subject,
    "time": now,
    "datacontenttype": "application/json",
    "dataschema": f"apicurio://holyfields/{ce_type}/versions/1",
    "correlationid": session_id,
    "causationid": session_id,
    "producer": "claude-code",
    "service": "claude-code",
    "domain": ce_type.split(".")[2],
    "schemaref": ce_type + ".v1",
    "traceparent": "00-00000000000000000000000000000000-0000000000000000-00",
    "kind": "event",
    "actor": {
        "type": "agent_cli",
        "agent_id": "bloodbank.agent.claude",
        "cli": "claude",
        "provider": "anthropic",
        "model": None,
    },
    "ordering_key": ordering_key,
    "data": json.loads(data_json),
}))
' "$ev_type" "$topic" "$SESSION_ID" "$NOW" "$ordering_key" "$data")

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

# Order matches the §14 sequence: session starts, turn starts, tool requested,
# tool invoked, subagent (agent invocation) completes, session ends.

publish_envelope \
  "bloodbank.v1.cli.session.started" \
  "bloodbank.evt.v1.cli.session.started" \
  "cli_session:${SESSION_ID}" \
  "{\"session_id\":\"${SESSION_ID}\",\"working_directory\":\"/tmp/smoketest\",\"git_branch\":\"main\",\"git_remote\":\"\",\"started_at\":\"${NOW}\"}"

publish_envelope \
  "bloodbank.v1.conversation.turn.started" \
  "bloodbank.evt.v1.conversation.turn.started" \
  "thread:${SESSION_ID}" \
  "{\"thread_id\":\"${SESSION_ID}\",\"turn_id\":\"${SESSION_ID}:1\",\"prompt_text\":\"list .claude/hooks\",\"prompt_length\":18,\"working_directory\":\"/tmp/smoketest\",\"git_branch\":\"main\"}"

publish_envelope \
  "bloodbank.v1.tool.tool_call.requested" \
  "bloodbank.evt.v1.tool.tool_call.requested" \
  "invocation:${SESSION_ID}" \
  "{\"invocation_id\":\"${SESSION_ID}\",\"tool_call_id\":\"tc-1\",\"tool_name\":\"Bash\",\"arguments\":{\"command\":\"ls .claude/hooks\"},\"working_directory\":\"/tmp/smoketest\",\"git_branch\":\"main\",\"turn_number\":1}"

publish_envelope \
  "bloodbank.v1.tool.tool_call.invoked" \
  "bloodbank.evt.v1.tool.tool_call.invoked" \
  "invocation:${SESSION_ID}" \
  "{\"invocation_id\":\"${SESSION_ID}\",\"tool_call_id\":\"tc-1\",\"tool_name\":\"Bash\",\"arguments\":{\"command\":\"ls .claude/hooks\"},\"working_directory\":\"/tmp/smoketest\",\"git_branch\":\"main\",\"git_status\":\"clean\",\"turn_number\":1,\"success\":true}"

publish_envelope \
  "bloodbank.v1.agent.invocation.completed" \
  "bloodbank.evt.v1.agent.invocation.completed" \
  "invocation:${SESSION_ID}" \
  "{\"invocation_id\":\"${SESSION_ID}\",\"agent_type\":\"general-purpose\",\"stop_reason\":\"completed\",\"working_directory\":\"/tmp/smoketest\"}"

publish_envelope \
  "bloodbank.v1.cli.session.ended" \
  "bloodbank.evt.v1.cli.session.ended" \
  "cli_session:${SESSION_ID}" \
  "{\"session_id\":\"${SESSION_ID}\",\"end_reason\":\"user_stop\",\"duration_seconds\":42,\"total_turns\":1,\"tools_used\":{\"Bash\":1},\"files_modified\":[],\"git_commits\":[],\"final_status\":\"success\",\"working_directory\":\"/tmp/smoketest\",\"git_branch\":\"main\"}"

# Wait for delivery
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

# Validate
RECEIVED_JSON="$(curl -sS --max-time 3 "${RECORDER_HTTP}/inspect/recorded" 2>/dev/null)"

python3 -c "
import json, re, sys
data = json.loads(sys.argv[1])
session_id = sys.argv[2]

problems = []
envelopes = data.get('envelopes', [])
count_by_type = data.get('count_by_type', {})
sessions = data.get('sessions', [])

expected_types = (
    'bloodbank.v1.cli.session.started',
    'bloodbank.v1.cli.session.ended',
    'bloodbank.v1.conversation.turn.started',
    'bloodbank.v1.tool.tool_call.requested',
    'bloodbank.v1.tool.tool_call.invoked',
    'bloodbank.v1.agent.invocation.completed',
)
for t in expected_types:
    if count_by_type.get(t, 0) < 1:
        problems.append(f'count_by_type missing or zero for {t}')

TYPE_RE = re.compile(r'^bloodbank\.v[0-9]+\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$')
for i, env in enumerate(envelopes):
    for required in ('specversion', 'id', 'source', 'type', 'time', 'correlationid', 'producer', 'service', 'domain', 'kind', 'actor', 'data'):
        if required not in env:
            problems.append(f'envelope[{i}] missing {required!r}')
    if env.get('specversion') != '1.0':
        problems.append(f\"envelope[{i}] specversion = {env.get('specversion')!r}\")
    if not isinstance(env.get('type'), str) or not TYPE_RE.match(env['type']):
        problems.append(f\"envelope[{i}] type {env.get('type')!r} fails v1 regex\")
    if env.get('kind') != 'event':
        problems.append(f\"envelope[{i}] kind = {env.get('kind')!r}, want 'event'\")
    if not isinstance(env.get('actor'), dict) or not env['actor'].get('agent_id'):
        problems.append(f\"envelope[{i}] actor missing or no agent_id\")
    if env.get('ordering_key') in (None, ''):
        problems.append(f\"envelope[{i}] missing ordering_key\")

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
