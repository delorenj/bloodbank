# Naming: CloudEvents type and NATS subject

Authoritative source: `~/code/33GOD/bloodbank/docs/event-naming.md`.

Bloodbank v1 separates the semantic event identity from the transport subject.
Get both right; downstream routing, schema validation, and JetStream stream
binding depend on it.

## CloudEvents `type`

Every event type is exactly five dotted tokens:

```text
bloodbank.v1.<domain>.<entity>.<action>
```

Examples:

| Meaning | CloudEvents `type` |
|---|---|
| Agent session started | `bloodbank.v1.agent.session.started` |
| Agent tool completed | `bloodbank.v1.agent.tool.completed` |
| Conversation turn started | `bloodbank.v1.conversation.turn.started` |
| System heartbeat received | `bloodbank.v1.system.heartbeat.received` |

Provider, CLI, model, repo, and agent IDs do **not** go in `type`; use `actor`,
`source`, envelope metadata, or `data`.

## NATS subject

Subjects mirror `type` but insert the transport kind after `bloodbank`:

```text
bloodbank.<kind>.v1.<domain>.<entity>.<action>
```

| Kind | Envelope `kind` | Subject prefix | Stream |
|---|---|---|---|
| `evt` | `event` | `bloodbank.evt.v1.` | `BLOODBANK_EVENTS` |
| `cmd` | `command` | `bloodbank.cmd.v1.` | `BLOODBANK_COMMANDS` |
| `rpy` | `reply` | `bloodbank.rpy.v1.` | `BLOODBANK_COMMANDS` |

Example:

```text
type     bloodbank.v1.agent.tool.completed
subject  bloodbank.evt.v1.agent.tool.completed
```

The subject's `(domain, entity, action)` must match `type` exactly. The kind
marker is transport routing; `envelope.kind` remains authoritative.

## Subject filters

- `bloodbank.evt.v1.agent.tool.completed` — exactly that event.
- `bloodbank.evt.v1.agent.>` — all agent events.
- `bloodbank.evt.v1.>` — event catch-all used by `bloodbank-event-toaster`.
- `bloodbank.cmd.v1.agent.invocation.start` — command to start an invocation;
  target agent lives in `data.target_agent_id`, not in the subject path.

The legacy `event.>` / `command.>` / `reply.>` prefixes are deprecated.

## Action verb tense

- Events: past tense / past participle (`started`, `ended`, `completed`,
  `failed`, `received`, `clocked_in`).
- Commands: imperative present (`start`, `complete`, `invoke`, `clock_in`).
- Replies: same action as the command they answer.

## Anti-patterns

- `agent.session.started` — missing `bloodbank.v1`.
- `bloodbank.v1.copilot.tool.completed` — provider encoded in `type`.
- `event.agent.tool.completed` — legacy subject prefix.
- `bloodbank.evt.v1.agent.tool.invoked` when the envelope `type` is
  `bloodbank.v1.agent.tool.completed`.
- Encoding target agent IDs in the command subject path.
