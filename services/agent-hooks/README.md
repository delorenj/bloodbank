# agent-hooks

Per-CLI bridges that turn agent-CLI lifecycle events (Claude Code, GitHub
Copilot CLI, …) into CloudEvents 1.0 envelopes on the Bloodbank NATS bus.

Each supported CLI gets a thin entry point under its own subdirectory; all
shared behavior — envelope construction, NATS publishing, session/causation
state — lives in [`core/`](./core).

## Layout

```
agent-hooks/
├── core/
│   ├── envelope.py       # build_envelope() — correlationid + causationid REQUIRED
│   ├── nats_publish.py   # stdlib-only NATS text-protocol PUB
│   └── session.py        # file-backed SessionState (correlation/causation chain)
├── claude/
│   └── publish.py        # entry point for Claude Code hooks
├── copilot/
│   ├── publish.py        # entry point for Copilot CLI hooks
│   └── hooks.json        # Copilot hooks config (symlinked into ~/.copilot/hooks/)
└── README.md
```

## Envelope contract

Every envelope conforms to `holyfields/schemas/_common/cloudevent_base.v1.json`.
Per `bloodbank/CLAUDE.md`, both `correlationid` and `causationid` are
mandatory; `core.envelope.build_envelope()` raises `ValueError` if either is
empty.

| Field | Source |
|---|---|
| `correlationid` | `SessionState.session_id` — stable for the CLI session |
| `causationid`   | `SessionState.last_event_id` — id of the previously published event |
| `id`            | new UUID (or `session_id` on the first event of a chain) |
| `source` / `producer` / `service` / `domain` | per-CLI constants |

The chain self-roots: on the session-start event, the entry point passes
`event_id=session_id` so the very first event's `id == correlationid ==
causationid`. Every subsequent event sets `causationid` to the previous
event's id, giving downstream consumers a linkable chain.

## Transport

NATS-direct on `127.0.0.1:4222` via a ~40-line stdlib socket client. No
nats-py, no virtualenv, no Dapr sidecar on the publish path. Justified by
the fire-and-forget shape of CLI hooks. Override via:

| env | default | purpose |
|---|---|---|
| `BLOODBANK_NATS_HOST`    | `127.0.0.1` | NATS host |
| `BLOODBANK_NATS_PORT`    | `4222`      | NATS port |
| `BLOODBANK_NATS_TIMEOUT` | `3.0`       | Connect/publish timeout (s) |
| `BLOODBANK_ENABLED`      | `true`      | `false` disables publishing entirely |
| `BLOODBANK_DEBUG`        | unset       | `true` logs each publish to stderr |
| `BLOODBANK_HOOK_VERBOSE` | unset       | when set, log `published <subject>` to stderr |
| `BLOODBANK_HOOK_STRICT`  | unset       | `1` exits non-zero on publish failure (default: fail open) |

## Claude Code

| Hook | event-type arg | CloudEvents type | NATS subject |
|---|---|---|---|
| `SessionStart`     | `session-start`    | `agent.session.started`    | `event.agent.session.started`    |
| `UserPromptSubmit` | `prompt-submitted` | `agent.prompt.submitted`   | `event.agent.prompt.submitted`   |
| `PreToolUse`       | `tool-request`     | `agent.tool.requested`     | `event.agent.tool.requested`     |
| `PostToolUse`      | `tool-action`      | `agent.tool.invoked`       | `event.agent.tool.invoked`       |
| `SubagentStop`     | `subagent-stopped` | `agent.subagent.completed` | `event.agent.subagent.completed` |
| `Stop`             | `session-end`      | `agent.session.ended`      | `event.agent.session.ended`      |

Session state lives at `~/.claude/bloodbank-session.json` (single global
session, cwd-independent). On `session-end` it's archived to
`~/.claude/bloodbank-sessions/<session_id>.json`. Per-event `working_directory`
and `git_branch` are captured live from cwd at event time, so each event
reflects where the tool action actually happened.

Install: registered in user-global `~/.claude/settings.json`. Fires for
**every** Claude Code session regardless of launch directory.

## Copilot CLI

Reference: <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-hooks>

| Copilot hook | NATS subject | CloudEvents type |
|---|---|---|
| `sessionStart`        | `event.copilot.session.started`  | `copilot.session.started`  |
| `sessionEnd`          | `event.copilot.session.ended`    | `copilot.session.ended`    |
| `userPromptSubmitted` | `event.copilot.prompt.submitted` | `copilot.prompt.submitted` |
| `preToolUse`          | `event.copilot.tool.pre`         | `copilot.tool.pre`         |
| `postToolUse`         | `event.copilot.tool.post`        | `copilot.tool.post`        |
| `errorOccurred`       | `event.copilot.error.occurred`   | `copilot.error.occurred`   |
| `agentStop`           | `event.copilot.agent.stopped`    | `copilot.agent.stopped`    |

Session state lives at `~/.copilot/bloodbank-session.json`.

Install:

```bash
mkdir -p ~/.copilot/hooks
ln -snf "$(pwd)/copilot/hooks.json" ~/.copilot/hooks/bloodbank.json
```

Copilot CLI picks up `~/.copilot/hooks/*.json` on next launch.

## Verify

With NATS up (`mise run up` from the bloodbank root):

```bash
# Single-hook smoke
echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' \
  | python3 services/agent-hooks/claude/publish.py tool-action

echo '{"probe":"sessionStart"}' \
  | BLOODBANK_HOOK_VERBOSE=1 python3 services/agent-hooks/copilot/publish.py sessionStart

# Tail the catch-all toaster, then trigger a hook in another shell.
docker logs -f bloodbank-event-toaster
```

End-to-end, the catch-all `event-toaster` forwards every envelope to
`https://ntfy.delo.sh/bloodbank` and the `claude-events-recorder` consumes
`event.agent.*` for its `/inspect/recorded` test hook.

## Adding a new CLI

1. Create `agent-hooks/<cli>/publish.py` that:
   - imports `core.envelope.build_envelope`, `core.nats_publish.publish`, `core.session.SessionState`,
   - picks a stable on-disk `SessionState` path (`~/.<cli>/bloodbank-session.json`),
   - maps the CLI's hook names to `(ce_type, nats_subject)`,
   - publishes via `nats_publish(...)` and updates `session.record_event(envelope["id"])` on success.
2. Add the CLI's hooks config (symlinkable into its hook directory).
3. Document the hook table here.

Anti-patterns:

- Don't widen the runtime deps past stdlib — that's why both publishers are
  single-file, drop-in usable from any agent CLI hook.
- Don't reintroduce a Dapr sidecar on the publish path. The bus is the
  contract; sidecars are an implementation detail of subscribers.
- Don't build envelopes by hand outside `core.envelope.build_envelope()`.
  The mandatory correlationid/causationid check exists precisely so a new
  CLI can't accidentally publish out-of-spec.
