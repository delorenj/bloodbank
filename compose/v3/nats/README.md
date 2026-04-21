# NATS JetStream topology (Bloodbank v3)

Machine-readable counterpart: `streams.json` in this directory.

This file documents the subject hierarchy, stream retention, replay
semantics, and dead-letter posture for the v3 event platform. The naming
invariants are locked by `docs/architecture/ADR-0001-v3-platform-pivot.md`
in the metarepo.

## Streams

| Stream                   | Subjects                    | Retention   | Storage | Max age | Discard |
|--------------------------|-----------------------------|-------------|---------|---------|---------|
| `BLOODBANK_V3_EVENTS`    | `event.>`                   | `limits`    | `file`  | `7d`    | `old`   |
| `BLOODBANK_V3_COMMANDS`  | `command.>`, `reply.>`      | `workqueue` | `file`  | `1d`    | `old`   |

- **Events** (`limits` retention) are durable CloudEvents facts. They are
  replayable, aged out after 7 days, and consumed by projections and
  downstream services.
- **Commands** (`workqueue` retention) are operational requests and their
  replies. They are handled once and aged out after 1 day. They are
  **not** replayable as source-of-truth history.

## Subject conventions

All subjects are lowercase, dot-separated, and use only ASCII `a-z`, `0-9`,
`-`, and `_` in each segment.

### Events: `event.<domain>.<entity>.<action>`

- `domain` — coarse-grained business area (e.g. `artifact`, `agent`,
  `workspace`, `meeting`, `dashboard`).
- `entity` — the aggregate root within the domain.
- `action` — past-tense verb describing the fact that happened.

Examples:

- `event.artifact.version.created`
- `event.agent.session.started`
- `event.workspace.worktree.deleted`

### Commands: `command.<target>.<verb>`

- `target` — the service expected to handle the command. Match the service
  name from `services/registry.yaml`.
- `verb` — imperative verb.

Examples:

- `command.artifact-service.rebuild`
- `command.workspace-service.clone-worktree`

### Replies: `reply.<target>.<verb>`

- Mirrors the command subject; used as `reply_to` on the command envelope.
- One reply subject may carry multiple reply types; consumers match on
  `correlation_id`.

Examples:

- `reply.artifact-service.rebuild`
- `reply.workspace-service.clone-worktree`

## Retention posture

- **Events:** 7-day rolling window, file-backed. Long enough to cover a
  weekend outage plus a deliberate replay window, short enough to keep disk
  use predictable on the sandbox host. Production tuning is a follow-up.
- **Commands:** 1-day rolling window, file-backed, `workqueue` retention so
  that once a consumer acks a command it is removed from the stream.

## Replay posture

Only `BLOODBANK_V3_EVENTS` is replayable. Replays are run by operator
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

3. **Route through Dapr `bloodbank-v3-pubsub`.** Replay tooling must not
   bypass the pub/sub abstraction, so consumer wiring stays uniform.

Commands are not replayable. If a command needs to be re-issued, the
operator issues a new command with a new `command_id` and a
`causation_id` referencing the original.

## Dead-letter posture

Dead-letter handling is **documented, not implemented** in the scaffold
wave. The expected model:

- Each durable consumer gets a paired dead-letter stream (e.g.
  `BLOODBANK_V3_EVENTS_DLQ_<consumer>`).
- A message lands there after exceeding `max_deliver` on the primary
  stream.
- Operator tooling (V3-007) provides inspection and redrive commands.
- DLQ streams inherit `file` storage, `limits` retention, and a longer
  `max_age` than the primary stream.

No DLQ infrastructure is provisioned from this directory in the current
scaffold. Adding it is V3-007's responsibility.

## Bootstrapping the streams

Stream creation is not automated in this scaffold. Operator tooling in
V3-005 / V3-006 will read `streams.json` and apply it via the NATS CLI
(`nats stream add --config ...`) or the JetStream API. Until then,
`streams.json` is the declarative source of truth and nothing writes to
the broker.
