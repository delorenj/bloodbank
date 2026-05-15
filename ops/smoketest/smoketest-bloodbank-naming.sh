#!/usr/bin/env bash
#
# Bloodbank Event Naming Contract v1 verifier (docs/event-naming.md §16.2).
#
# Walks the canonical §14 agent-turn sequence for both Claude and Copilot
# actors, runs each synthetic envelope through `cli/bb.py verify-envelope`,
# and asserts that:
#
#   - `type` matches §2's 5-token regex
#   - `kind` ∈ {event, command, reply}
#   - `subject` mirrors `type` via the kind marker (§3)
#   - domain/entity/action are in the v1 allowlists (§6–§8)
#   - no banned tokens in `type` (§9)
#   - actor, ordering_key (event), correlationid, causationid all present
#
# Pure contract verifier. Stdlib-only: NATS/Dapr/Docker are NOT required.
# Use `smoketest-claude-events.sh` for end-to-end transport verification.
#
# Exit codes:
#   0 — PASS (every envelope satisfies the contract for both actors)
#   1 — at least one envelope failed contract validation
#   2 — unexpected error (e.g. cli/bb.py not runnable)
#
# Usage:
#   bash ops/smoketest/smoketest-bloodbank-naming.sh

set -euo pipefail

BLOODBANK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BB="${BB:-python3 ${BLOODBANK_ROOT}/cli/bb.py}"

fail_count=0
pass_count=0

# Each row: ce_type | ordering_bucket_prefix | data_json
SEQUENCE=(
  "bloodbank.v1.conversation.thread.created|thread|{\"thread_id\":\"thr-1\"}"
  "bloodbank.v1.conversation.turn.started|thread|{\"thread_id\":\"thr-1\",\"turn_id\":\"turn-1\"}"
  "bloodbank.v1.agent.invocation.started|invocation|{\"invocation_id\":\"inv-1\"}"
  "bloodbank.v1.cli.session.started|cli_session|{\"session_id\":\"sess-1\"}"
  "bloodbank.v1.cli.process.spawned|process|{\"session_id\":\"sess-1\",\"process_id\":\"proc-1\"}"
  "bloodbank.v1.cli.stdout.appended|process|{\"session_id\":\"sess-1\",\"process_id\":\"proc-1\",\"chunk\":\"hello\"}"
  "bloodbank.v1.cli.stderr.appended|process|{\"session_id\":\"sess-1\",\"process_id\":\"proc-1\",\"chunk\":\"warn\"}"
  "bloodbank.v1.llm.request.sent|invocation|{\"invocation_id\":\"inv-1\",\"request_id\":\"req-1\"}"
  "bloodbank.v1.llm.response.received|invocation|{\"invocation_id\":\"inv-1\",\"response_id\":\"resp-1\"}"
  "bloodbank.v1.tool.tool_call.requested|invocation|{\"invocation_id\":\"inv-1\",\"tool_call_id\":\"tc-1\",\"tool_name\":\"Bash\"}"
  "bloodbank.v1.tool.tool_call.invoked|invocation|{\"invocation_id\":\"inv-1\",\"tool_call_id\":\"tc-1\",\"tool_name\":\"Bash\"}"
  "bloodbank.v1.tool.tool_call.completed|invocation|{\"invocation_id\":\"inv-1\",\"tool_call_id\":\"tc-1\",\"tool_name\":\"Bash\",\"outcome\":\"success\"}"
  "bloodbank.v1.conversation.message.appended|turn|{\"thread_id\":\"thr-1\",\"turn_id\":\"turn-1\",\"message_id\":\"msg-1\",\"role\":\"assistant\"}"
  "bloodbank.v1.agent.invocation.completed|invocation|{\"invocation_id\":\"inv-1\"}"
  "bloodbank.v1.conversation.turn.completed|thread|{\"thread_id\":\"thr-1\",\"turn_id\":\"turn-1\",\"outcome\":\"completed\"}"
)

# Each actor: (cli, provider) tuple.
ACTORS=(
  "claude|anthropic"
  "copilot|github_copilot"
)

verify_one() {
  local ce_type="$1"
  local bucket="$2"
  local data_json="$3"
  local cli="$4"
  local provider="$5"
  local envelope
  envelope=$(python3 -c '
import json, sys, uuid, datetime
ce_type, bucket, data_json, cli, provider = sys.argv[1:]
print(json.dumps({
    "specversion": "1.0",
    "id": str(uuid.uuid4()),
    "source": f"urn:33god:agent:{cli}",
    "type": ce_type,
    "subject": f"bloodbank.evt.v1.{ce_type.split(chr(46), 2)[2]}",
    "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    "datacontenttype": "application/json",
    "dataschema": f"apicurio://holyfields/{ce_type}/versions/1",
    "correlationid": "00000000-0000-0000-0000-000000000001",
    "causationid": "00000000-0000-0000-0000-000000000001",
    "producer": cli,
    "service": cli,
    "domain": ce_type.split(".")[2],
    "schemaref": ce_type + ".v1",
    "traceparent": "00-00000000000000000000000000000000-0000000000000000-00",
    "kind": "event",
    "actor": {"type": "agent_cli", "agent_id": f"bloodbank.agent.{cli}", "cli": cli, "provider": provider, "model": None},
    "ordering_key": f"{bucket}:smoketest",
    "data": json.loads(data_json),
}))
' "$ce_type" "$bucket" "$data_json" "$cli" "$provider")

  if printf '%s' "$envelope" | $BB verify-envelope >/dev/null 2>&1; then
    pass_count=$((pass_count + 1))
    echo "PASS [${cli}] ${ce_type}"
  else
    fail_count=$((fail_count + 1))
    echo "FAIL [${cli}] ${ce_type}"
    printf '%s' "$envelope" | $BB verify-envelope || true
  fi
}

echo "smoketest-bloodbank-naming: verifying §14 sequence for ${#ACTORS[@]} actors × ${#SEQUENCE[@]} events"

for actor in "${ACTORS[@]}"; do
  IFS='|' read -r cli provider <<<"$actor"
  for row in "${SEQUENCE[@]}"; do
    IFS='|' read -r ce_type bucket data_json <<<"$row"
    verify_one "$ce_type" "$bucket" "$data_json" "$cli" "$provider"
  done
done

# Negative-case probes: each MUST be rejected.
echo "smoketest-bloodbank-naming: negative-case probes"

negative_probe() {
  local label="$1"
  local envelope_json="$2"
  if printf '%s' "$envelope_json" | $BB verify-envelope >/dev/null 2>&1; then
    fail_count=$((fail_count + 1))
    echo "FAIL negative-probe accepted: ${label}"
  else
    pass_count=$((pass_count + 1))
    echo "PASS negative-probe rejected: ${label}"
  fi
}

LEGACY_3TOKEN='{"specversion":"1.0","id":"a","source":"x","type":"agent.session.started","time":"2026-05-14T00:00:00Z","correlationid":"c","producer":"p","service":"s","domain":"agent","kind":"event","actor":{"type":"t","agent_id":"a"},"ordering_key":"k","data":{}}'
negative_probe "legacy 3-token type (agent.session.started)" "$LEGACY_3TOKEN"

BANNED_CLAUDE='{"specversion":"1.0","id":"a","source":"x","type":"bloodbank.v1.agent.claude.completed","time":"2026-05-14T00:00:00Z","correlationid":"c","producer":"p","service":"s","domain":"agent","kind":"event","actor":{"type":"t","agent_id":"a"},"ordering_key":"k","data":{}}'
negative_probe "banned token 'claude' in entity slot" "$BANNED_CLAUDE"

WRONG_TENSE='{"specversion":"1.0","id":"a","source":"x","type":"bloodbank.v1.conversation.message.append","time":"2026-05-14T00:00:00Z","correlationid":"c","producer":"p","service":"s","domain":"conversation","kind":"event","actor":{"type":"t","agent_id":"a"},"ordering_key":"k","data":{}}'
negative_probe "imperative 'append' on kind=event" "$WRONG_TENSE"

MISSING_ACTOR='{"specversion":"1.0","id":"a","source":"x","type":"bloodbank.v1.conversation.message.appended","time":"2026-05-14T00:00:00Z","correlationid":"c","producer":"p","service":"s","domain":"conversation","kind":"event","ordering_key":"k","data":{}}'
negative_probe "missing actor on kind=event" "$MISSING_ACTOR"

SUBJECT_MISMATCH='{"specversion":"1.0","id":"a","source":"x","type":"bloodbank.v1.conversation.message.appended","subject":"bloodbank.cmd.v1.conversation.message.appended","time":"2026-05-14T00:00:00Z","correlationid":"c","producer":"p","service":"s","domain":"conversation","kind":"event","actor":{"type":"t","agent_id":"a"},"ordering_key":"k","data":{}}'
negative_probe "subject kind marker mismatch (cmd on kind=event)" "$SUBJECT_MISMATCH"

total=$((pass_count + fail_count))
echo "smoketest-bloodbank-naming: ${pass_count}/${total} checks passed"

if [[ ${fail_count} -gt 0 ]]; then
  echo "smoketest-bloodbank-naming: FAIL (${fail_count} failures)" >&2
  exit 1
fi
echo "smoketest-bloodbank-naming: PASS"
exit 0
