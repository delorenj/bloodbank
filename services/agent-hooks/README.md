# agent-hooks

Per-CLI bridges that turn agent-CLI lifecycle events (Claude Code, GitHub
Copilot CLI, OpenClaw, …) into CloudEvents 1.0 envelopes on the Bloodbank
NATS bus, conforming to the **Bloodbank Event Naming Contract v1** at
`bloodbank/docs/event-naming.md`.

Each supported CLI gets a thin entry point under its own subdirectory; all
shared behavior — envelope construction, NATS publishing, session/causation
state, contract validation — lives in [`core/`](./core).

## Layout

```
agent-hooks/
├── core/
│   ├── envelope.py       # build_envelope() — enforces v1 contract on every call
│   ├── validate.py       # §2 regex, §5 tense, §6–§9 allowlists, §11 fields
│   ├── nats_publish.py   # stdlib-only NATS text-protocol PUB
│   └── session.py        # file-backed SessionState (correlation/causation chain)
├── claude/
│   └── publish.py        # entry point for Claude Code hooks
├── copilot/
│   ├── publish.py        # entry point for Copilot CLI hooks
│   └── hooks.json        # Copilot hooks config (symlinked into ~/.copilot/hooks/)
├── openclaw/
│   └── watch.py          # tail OpenClaw session logs → Bloodbank
└── README.md
```

## Envelope contract

Every envelope conforms to `bloodbank/schemas/_common/cloudevent_base.v1.json`
and the v1 naming contract. `core.envelope.build_envelope()` raises
`ContractViolation` on any deviation. There is no legacy 3-token path and
no compat aliases — see "Hard rename, no aliases" in
`docs/event-naming.md` §15.

| Field             | Source                                                                          |
|-------------------|---------------------------------------------------------------------------------|
| `type`            | 5-token `bloodbank.v1.<domain>.<entity>.<action>` — validated against §2 regex  |
| `subject`         | derived: `bloodbank.<kind>.v1.<...>` — validated against §3                     |
| `kind`            | `event` (hooks emit only events; commands flow through a different path)        |
| `actor`           | per-CLI constant (`actor.cli`, `actor.provider`); §10                           |
| `correlationid`   | `SessionState.session_id` — stable for the CLI session                          |
| `causationid`     | `SessionState.last_event_id` — id of the previously published event             |
| `ordering_key`    | `<bucket>:<session_id>` where bucket ∈ {cli_session, thread, invocation, …}     |
| `id`              | new UUID (or `session_id` on the first event of a chain)                        |
| `source` / `producer` / `service` | per-CLI constants                                                       |

The chain self-roots: on the session-start event the entry point passes
`event_id=session_id` so the first event's `id == correlationid ==
causationid`. Every subsequent event sets `causationid` to the previous
event's id, giving downstream consumers a linkable chain.

Provider/CLI/model identity goes into `actor.cli`, `actor.provider`,
`actor.model` — and is **banned from `type`** (§9).

## Transport

NATS-direct on `127.0.0.1:4222` via a ~40-line stdlib socket client. No
nats-py, no virtualenv, no Dapr sidecar on the publish path. Justified by
the fire-and-forget shape of CLI hooks. Override via:

| env                      | default     | purpose                                                                 |
|--------------------------|-------------|-------------------------------------------------------------------------|
| `BLOODBANK_NATS_HOST`    | `127.0.0.1` | NATS host                                                               |
| `BLOODBANK_NATS_PORT`    | `4222`      | NATS port                                                               |
| `BLOODBANK_NATS_TIMEOUT` | `3.0`       | Connect/publish timeout (s)                                             |
| `BLOODBANK_ENABLED`      | `true`      | `false` disables publishing entirely                                    |
| `BLOODBANK_DEBUG`        | unset       | `true` logs each publish to stderr                                      |
| `BLOODBANK_HOOK_VERBOSE` | unset       | when set, log `published <subject>` to stderr                           |
| `BLOODBANK_HOOK_STRICT`  | unset       | `1` exits non-zero on publish failure (default: fail open)              |
| `BLOODBANK_HOOK_VALIDATE`| unset       | `1` runs JSON Schema validation against the matching `bloodbank/schemas/` schema  |
| `BLOODBANK_SCHEMAS_DIR`  | unset       | overrides the schema-tree lookup; defaults to repo-local `bloodbank/schemas/`     |

## Claude Code

| Hook              | event-type arg     | v1 CloudEvents `type`                       | NATS subject                                       |
|-------------------|--------------------|---------------------------------------------|----------------------------------------------------|
| `SessionStart`    | `session-start`    | `bloodbank.v1.cli.session.started`          | `bloodbank.evt.v1.cli.session.started`             |
| `UserPromptSubmit`| `prompt-submitted` | `bloodbank.v1.conversation.turn.started`    | `bloodbank.evt.v1.conversation.turn.started`       |
| `PreToolUse`      | `tool-request`     | `bloodbank.v1.tool.tool_call.requested`     | `bloodbank.evt.v1.tool.tool_call.requested`        |
| `PostToolUse`     | `tool-action`      | `bloodbank.v1.tool.tool_call.invoked`       | `bloodbank.evt.v1.tool.tool_call.invoked`          |
| `SubagentStop`    | `subagent-stopped` | `bloodbank.v1.agent.invocation.completed`   | `bloodbank.evt.v1.agent.invocation.completed`      |
| `Stop`            | `session-end`      | `bloodbank.v1.cli.session.ended`            | `bloodbank.evt.v1.cli.session.ended`               |

Session state lives at `~/.claude/bloodbank-session.json` (single global
session, cwd-independent). On `session-end` it's archived to
`~/.claude/bloodbank-sessions/<session_id>.json`. Per-event `working_directory`
and `git_branch` are captured live from cwd at event time.

`actor` constant: `cli=claude`, `provider=anthropic`, `model=null`.

Install: registered in user-global `~/.claude/settings.json`. Fires for
**every** Claude Code session regardless of launch directory.

## Copilot CLI

Reference: <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-hooks>

| Copilot hook         | v1 CloudEvents `type`                       | NATS subject                                       |
|----------------------|---------------------------------------------|----------------------------------------------------|
| `sessionStart`       | `bloodbank.v1.cli.session.started`          | `bloodbank.evt.v1.cli.session.started`             |
| `sessionEnd`         | `bloodbank.v1.cli.session.ended`            | `bloodbank.evt.v1.cli.session.ended`               |
| `userPromptSubmitted`| `bloodbank.v1.conversation.turn.started`    | `bloodbank.evt.v1.conversation.turn.started`       |
| `preToolUse`         | `bloodbank.v1.tool.tool_call.requested`     | `bloodbank.evt.v1.tool.tool_call.requested`        |
| `postToolUse`        | `bloodbank.v1.tool.tool_call.completed`     | `bloodbank.evt.v1.tool.tool_call.completed`        |
| `errorOccurred`      | `bloodbank.v1.agent.invocation.failed`      | `bloodbank.evt.v1.agent.invocation.failed`         |
| `agentStop`          | `bloodbank.v1.agent.invocation.completed`   | `bloodbank.evt.v1.agent.invocation.completed`      |

Session state lives at `~/.copilot/bloodbank-session.json`.

`actor` constant: `cli=copilot`, `provider=github_copilot`, `model=null`.

Install:

```bash
mkdir -p ~/.copilot/hooks
ln -snf "$(pwd)/copilot/hooks.json" ~/.copilot/hooks/bloodbank.json
```

Copilot CLI picks up `~/.copilot/hooks/*.json` on next launch.

## OpenClaw

`openclaw/watch.py` tails OpenClaw session logs and trajectory files,
synthesizing the same v1 event shapes (`cli.session.*`,
`conversation.turn.started`, `tool.tool_call.*`, `agent.invocation.*`).
`actor.cli=openclaw`, `actor.agent_id=bloodbank.agent.openclaw.<per-session-agent-id>`.

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
`bloodbank.evt.v1.>` for its `/inspect/recorded` test hook.

## Contract verifier

```bash
mise run smoketest:bloodbank-naming
```

Pure stdlib-only verifier — no Docker/NATS required. Runs every event in the
§14 canonical sequence through `cli/bb.py verify-envelope` for both Claude
and Copilot actors plus a set of negative-case probes.

## Adding a new CLI

1. Create `agent-hooks/<cli>/publish.py` that:
   - imports `core.envelope.build_envelope`, `core.nats_publish.publish`, `core.session.SessionState`,
   - picks a stable on-disk `SessionState` path (`~/.<cli>/bloodbank-session.json`),
   - maps the CLI's hook names to a v1 `<ce_type>` per `docs/event-naming.md` §15,
   - declares a per-CLI `actor` constant (`type`, `agent_id`, `cli`, `provider`, `model`),
   - publishes via `nats_publish(...)` and updates `session.record_event(envelope["id"])` on success.
2. Add the CLI's hooks config (symlinkable into its hook directory).
3. Document the hook table here.
4. If the CLI introduces a new provider name, add it to the banned-token
   list in `docs/event-naming.md` §9 and `core.validate.BANNED_TOKENS`.

Anti-patterns:

- Don't widen the runtime deps past stdlib — that's why every publisher is
  single-file, drop-in usable from any agent CLI hook.
- Don't reintroduce a Dapr sidecar on the publish path. The bus is the
  contract; sidecars are an implementation detail of subscribers.
- Don't build envelopes by hand outside `core.envelope.build_envelope()`.
  Contract enforcement (regex, tense, banned tokens) runs there.
- Don't put provider, CLI, or model names in `type`. They live in `actor`.
