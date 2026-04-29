# claude-events-recorder

Subscribes to all three `agent.*` events emitted by Claude Code via the
metarepo's `.claude/hooks/bloodbank-publisher.sh` and records them in
memory for inspection. The producer side runs on the host (Claude
Code itself); this is the consumer-side bookend, mirroring the
heartbeat-tick / heartbeat-recorder pattern.

## Architecture

```
Host: Claude Code
   ↓ (PostToolUse / Stop / SessionStart hook fires)
host: .claude/hooks/bloodbank-publisher.sh
   ↓ (POST CloudEvents 1.0 envelope, application/cloudevents+json)
host:3503 → daprd-claude-events sidecar
   ↓ (pubsub.jetstream)
NATS BLOODBANK_V3_EVENTS stream, subjects event.agent.*
   ↓ (Dapr delivers to --app-port)
container: claude-events-recorder
   ↓ (POST /events/{session_started,session_ended,tool_invoked})
in-memory buffer + per-session aggregate
```

## Subscriptions

The recorder declares one programmatic subscription per agent.* event
type. Explicit routes (no wildcard) keep the contract debuggable and
match the publisher's topic choices 1:1.

| CloudEvents `type` | NATS subject | Route |
|---|---|---|
| `agent.session.started`  | `event.agent.session.started`  | `/events/session_started`  |
| `agent.session.ended`    | `event.agent.session.ended`    | `/events/session_ended`    |
| `agent.prompt.submitted` | `event.agent.prompt.submitted` | `/events/prompt_submitted` |
| `agent.tool.requested`   | `event.agent.tool.requested`   | `/events/tool_requested`   |
| `agent.tool.invoked`     | `event.agent.tool.invoked`     | `/events/tool_invoked`     |
| `agent.subagent.completed` | `event.agent.subagent.completed` | `/events/subagent_completed` |

## Inspection endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/healthz`           | Liveness (204) |
| GET  | `/dapr/subscribe`    | Dapr subscribe contract |
| GET  | `/inspect/recorded`  | `{count, count_by_type, sessions, envelopes}` |
| POST | `/inspect/reset`     | Clear buffer (returns `{cleared}`) |
| POST | `/events/...`        | Dapr delivery — internal, not for direct callers |

The `sessions` array contains a per-session aggregate so tests can
assert lifecycle without walking the envelope buffer:

```json
{
  "session_id": "...",
  "started": true,
  "ended": true,
  "tool_invocations": 4,
  "tool_requests": 4,
  "prompts_submitted": 2,
  "subagents_completed": 1,
  "last_seen": "2026-04-29T08:01:23Z",
  "first_seen_type": "agent.session.started",
  "working_directory": "...",
  "git_branch": "...",
  "end_reason": "user_stop"
}
```

## Configuration (env vars)

| var               | default                | purpose |
|-------------------|------------------------|---------|
| `APP_PORT`        | `3001`                 | Container HTTP port |
| `SUBSCRIBE_PUBSUB`| `bloodbank-v3-pubsub`  | Dapr pubsub component |
| `MAX_BUFFER`      | `1024`                 | FIFO buffer cap |

## Running

The compose `claude-events` profile brings up the recorder + sidecar
(no producer container; Claude Code on the host is the producer):

```bash
docker compose --project-name bloodbank-v3 \
  --profile claude-events \
  -f compose/v3/docker-compose.yml \
  up -d nats nats-init dapr-placement claude-events-recorder daprd-claude-events
```

Verify the recorder is up:

```bash
curl http://127.0.0.1:3602/healthz   # 204
curl http://127.0.0.1:3602/inspect/recorded | jq
```

Then trigger a Claude Code hook (or run any tool through Claude Code)
and observe the count climb. Or fire a synthetic envelope:

```bash
bash ops/v3/smoketest/smoketest-claude-events.sh
```

## Why a separate profile from heartbeat

The heartbeat sidecar can technically serve publish-only workloads
(Dapr publish is generic). Splitting into a dedicated `claude-events`
profile gives:

1. A stable host port (3503) the publisher hook points at by default.
2. A subscriber tied specifically to `agent.*` events for
   query/inspection.
3. Clear separation of concerns when both profiles run side by side.
4. A smoke test that gates the agent.* round-trip in CI.
