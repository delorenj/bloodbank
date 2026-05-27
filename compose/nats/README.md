# NATS JetStream topology (Bloodbank)

Machine-readable counterpart: `streams.json` in this directory.

Authoritative naming contract: **`bloodbank/docs/event-naming.md`**. Any
conflict between this file and the contract is a defect here — fix this
file. ADR-0001 (metarepo, TBD) locks the stream names and `bloodbank-pubsub`
component name; everything else flows from the v1 contract.

## Streams

| Stream                | Subjects                                              | Retention   | Storage | Max age | Discard |
|-----------------------|-------------------------------------------------------|-------------|---------|---------|---------|
| `BLOODBANK_EVENTS`    | `bloodbank.evt.v1.>`                                  | `limits`    | `file`  | `7d`    | `old`   |
| `BLOODBANK_COMMANDS`  | `bloodbank.cmd.v1.>`, `bloodbank.rpy.v1.>`            | `workqueue` | `file`  | `1d`    | `old`   |

- **Events** (`limits` retention) are durable CloudEvents facts. They are
  replayable, aged out after 7 days, and consumed by projections and
  downstream services.
- **Commands** (`workqueue` retention) are operational requests and their
  replies. They are handled once and aged out after 1 day. They are
  **not** replayable as source-of-truth history.

## Subject conventions

Per `bloodbank/docs/event-naming.md` §3:

```
bloodbank.<kind>.v<N>.<domain>.<entity>.<action>
```

where `<kind>` ∈ `{evt, cmd, rpy}`. Six dot-separated tokens, all lowercase
`[a-z][a-z0-9_]*`. The corresponding CloudEvents `type` drops the `<kind>`
marker:

```
bloodbank.v<N>.<domain>.<entity>.<action>          (5 tokens)
```

### Events: `bloodbank.evt.v1.<domain>.<entity>.<action>`

Past-tense action. Examples:

- `bloodbank.evt.v1.conversation.message.appended`
- `bloodbank.evt.v1.cli.session.started`
- `bloodbank.evt.v1.system.heartbeat.received`

### Commands: `bloodbank.cmd.v1.<domain>.<entity>.<action>`

Imperative action. Examples:

- `bloodbank.cmd.v1.agent.invocation.start` (canonical PM->agent invocation command; route via `data.target_agent_id`)
- `bloodbank.cmd.v1.cli.process.spawn`

### Replies: `bloodbank.rpy.v1.<domain>.<entity>.<action>`

Mirrors the command action; used as `reply_to` on the command envelope.
Consumers correlate via `correlationid` / `in_reply_to`.

Examples:

- `bloodbank.rpy.v1.agent.invocation.start`
- `bloodbank.rpy.v1.cli.process.spawn`

For the domain / entity / action allowlists and banned-token rules, see
`docs/event-naming.md` §6–§9.

## Retention posture

- **Events:** 7-day rolling window, file-backed. Long enough to cover a
  weekend outage plus a deliberate replay window, short enough to keep disk
  use predictable on the sandbox host. Production tuning is a follow-up.
- **Commands:** 1-day rolling window, file-backed, `workqueue` retention so
  that once a consumer acks a command it is removed from the stream.

## Replay posture

Only `BLOODBANK_EVENTS` is replayable. Replays are run by operator
tooling (tracked in V3-007) and must:

1. **Preserve original event IDs.** The CloudEvents `id`, `correlationid`,
   `causationid`, and `source` fields are copied verbatim from the original
   message. Consumers must therefore be idempotent on `id`.
2. **Tag replays via NATS headers**, never by mutating the CloudEvents
   envelope. The scaffold reserves the following header names (matching
   `replay_posture.metadata_headers` in `streams.json`):

   | Header                      | Purpose                                                     |
   |-----------------------------|-------------------------------------------------------------|
   | `Bb-Replay`                 | `"true"` when this delivery is a replay.                    |
   | `Bb-Replay-Batch-Id`        | UUID identifying the replay batch.                          |
   | `Bb-Replay-Reason`          | Free-form operator-supplied reason, e.g. `projection-rebuild`. |
   | `Bb-Original-Publish-Time`  | RFC3339 timestamp of the original (non-replay) publish.     |

3. **Route through Dapr `bloodbank-pubsub`.** Replay tooling must not
   bypass the pub/sub abstraction, so consumer wiring stays uniform.

Commands are not replayable. If a command needs to be re-issued, the
operator issues a new command with a new `command_id` and a
`causation_id` referencing the original.

## Dead-letter posture

Dead-letter handling is **documented, not implemented** in the scaffold
wave. The expected model:

- Each durable consumer gets a paired dead-letter stream (e.g.
  `BLOODBANK_EVENTS_DLQ_<consumer>`).
- A message lands there after exceeding `max_deliver` on the primary
  stream.
- Operator tooling (V3-007) provides inspection and redrive commands.
- DLQ streams inherit `file` storage, `limits` retention, and a longer
  `max_age` than the primary stream.

No DLQ infrastructure is provisioned from this directory in the current
scaffold. Adding it is V3-007's responsibility.

## Bootstrapping the streams

`compose/nats/init.sh` reads `streams.json` and applies each stream
definition via the `nats` CLI on container startup (the `nats-init`
one-shot service in `compose/docker-compose.yml`). Idempotent — re-running
with existing streams is a no-op.
