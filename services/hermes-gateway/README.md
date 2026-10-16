# Bloodbank Hermes gateway

This directory is the Bloodbank-owned, standalone Hermes platform plugin for
the canonical `bloodbank.cmd.agent.invocation.start` command. It does not
modify or monkey-patch Hermes core.

The adapter binds one JetStream durable pull consumer, validates every command
before dispatch, maps `data.target_agent_id` to an existing Hermes profile,
and stamps that profile on an internal `MessageEvent`. A command is acknowledged
only after the Hermes processing-complete hook and terminal lifecycle event
publishes both finish. Malformed or unroutable poison messages receive a
JetStream terminal acknowledgement; transient registry, broker, publication,
or Hermes failures receive a delayed negative acknowledgement.

## Hard rollout gate

Historical (DNET-4): the review snapshot reported **zero currently eligible
entries** in the live fleet registry, and the then-strict default-deny would
have made every registry-derived target unroutable. Since 2026-09-22 an absent
`bloodbank.enabled` means enabled, so a routing record without the key is
eligible; the backfill note below is kept for the record. PJangler and
the Hermes template must first project the accepted `metadata.bloodbank`
contract, an explicitly approved backfill must enable the intended agents, and
a read-only audit must show the expected nonzero eligible set. Only then may an
operator deploy/restart the fleet gateway and run a bounded live command proof.
This repository change performs none of those rollout actions.

## Installation

Hermes now runs from a PM-managed environment that is rebuilt on every
`hermes update`; anything pip-installed by hand is dropped (this is what silently
killed command consumption on 2026-10-13). Do NOT `pip install` this package.
Instead install the vendored directory plugin, which carries the adapter code
and declares `nats-py`/`PyYAML` in `plugin.yaml` `pip_dependencies` so PM keeps
them in the shared environment:

```bash
services/hermes-gateway/hermes-plugin/install.sh   # links ~/.hermes/plugins/bloodbank-platform, runs `hermes pm install`
systemctl --user restart hermes-gateway.service
```

Verify: `~/.hermes/gateway_state.json` `.platforms.bloodbank.state == "connected"`
and `curl -s 'http://127.0.0.1:8222/jsz?consumers=true'` shows the
`bloodbank-hermes-gateway` consumer with activity (`num_waiting >= 1` when idle).
With the interactive lane enabled (below) it also shows
`bloodbank-hermes-gateway-interactive`, `max_ack_pending: 2`, `num_waiting >= 1`.

The legacy pip layout below is kept for reference only.

The package advertises the `bloodbank-platform` entry point in the
`hermes_agent.plugins` group. Enable that plugin in the host/default Hermes
home, then configure the platform in its `config.yaml`:

```yaml
plugins:
  enabled:
    - bloodbank-platform

gateway:
  multiplex_profiles: true
  multiplex_secondary_adapters: false
  platforms:
    bloodbank:
      enabled: true
      typing_indicator: false
      extra:
        target_profiles: {}
        fleet_registry: ~/.hermes/agents-registry.yaml
        execution_state_file: ~/.hermes/bloodbank-hermes-gateway-state.sqlite3
        allow_direct_profile_targets: false
        durable_name: bloodbank-hermes-gateway
        max_inflight: 4
        ack_wait_seconds: 90
        ack_progress_seconds: 20
        # Optional second lane; absent or `enabled: false` = single-lane gateway.
        interactive:
          enabled: true
          durable_name: bloodbank-hermes-gateway-interactive
          max_inflight: 2
          max_deliver: 8
          ack_wait_seconds: 180
          ack_progress_seconds: 30
          nak_delay_seconds: 3
          # Wall clock for ONE Hermes turn on this lane. Absent = 300. A strict
          # positive number of seconds (not a bool, string or null), at most 86400.
          turn_timeout_seconds: 300
```

Resolution precedence is the explicit `target_profiles` mapping, followed by
the fleet registry. The registry document itself must be a YAML mapping with
integer `schema_version: 1` and an explicit `agents` mapping; `agents: {}` is a
valid, empty registry. Empty documents, missing `agents`, unsupported versions,
non-string/blank agent keys, and non-mapping agent records invalidate the whole
snapshot. Agent keys must already match the canonical lowercase slug grammar
`^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`; padded, case-variant, or otherwise invalid
keys invalidate the snapshot rather than being normalized. Structural and
identity failures are transient and are never skipped or allowed to fall
through to direct-profile routing.

A registry-derived route is eligible only when all of these conditions hold
exactly:

- `agents.<agent_id>.profile_name` is a nonblank string;
- `agents.<agent_id>.bloodbank` is a mapping;
- `bloodbank.enabled` is **absent** or the YAML boolean `true` (no key means
  enabled; only an explicit `false` quarantines the agent);
- `bloodbank.gateway_scope == "fleet"`;
- `bloodbank.target_agent_id == <agent_id>`.

A present `bloodbank.enabled` that is not a YAML boolean (`"true"`, `yes` as a
string, `null`, `1`) is invalid: the row is treated as disabled and the gateway
logs an ERROR naming `agents.<agent_id>.bloodbank.enabled`. A missing
`bloodbank` mapping, an explicit `false`, or a mismatched scope/target is not
routable. The
gateway does not infer eligibility from Telegram configuration, systemd units,
lifecycle state, or profile existence. It reloads and rechecks the registry
after durable claim and again immediately before Hermes dispatch, including
restart delivery of an existing `pending` or `started` row. A policy-disabled
route from a valid registry is poison and receives a terminal acknowledgement;
a missing, unreadable, or structurally invalid registry remains transient and
is negatively acknowledged for retry.

`target_profiles` is an intentional high-trust operator override: a target in
that static mapping does not consult registry Bloodbank metadata. Keep the map
empty when registry policy should govern the route. Direct routing where the
external target exactly equals an existing profile is disabled by default and
is available only with `allow_direct_profile_targets: true` and only when that
target is absent from a valid registry; it cannot bypass an ineligible registry
record. Unknown targets never fall back to the default profile.

`BLOODBANK_NATS_URL` overrides `extra.nats_url`. `BLOODBANK_NATS_CREDS` may
point at a credentials file; credentials belong in runtime secret injection,
not tracked YAML.

## Lanes: batch and interactive

Two command types carry the same `data` payload, each through its own durable
pull consumer on `BLOODBANK_COMMANDS` and its own in-flight slots:

| lane | type / subject | durable | slots | for |
| --- | --- | --- | --- | --- |
| batch | `bloodbank.agent.invocation.start` / `bloodbank.cmd.agent.invocation.start` | `bloodbank-hermes-gateway` | `max_inflight` (4) | unattended work: ticket grooming, delegation, PM fan-out |
| interactive | `bloodbank.agent.invocation.invoke` / `bloodbank.cmd.agent.invocation.invoke` | `bloodbank-hermes-gateway-interactive` | `interactive.max_inflight` (2) | a human is waiting: a voice memo, a chat nudge |

A grooming burst that holds all four batch slots no longer delays an
interactive command: the lanes share only the NATS connection and the execution
journal, each has its own fetch loop, and total concurrency is the sum of the
slots (6 turns). Hermes runs distinct sessions of one profile concurrently, so
a busy profile does not block an interactive turn on a different `thread_id`.

- Enabled only by `extra.interactive.enabled: true` (strict boolean). Absent
  means the single-lane gateway exactly as before, so the code can ship before
  the config. Rollback is `enabled: false` plus a gateway restart.
- The adapter creates both durables on `connect()` and refuses a stale durable
  whose filter, `max_ack_pending` or `max_deliver` differs (delete the durable
  once and restart). If either lane cannot bind, the platform reports
  `bloodbank_connect_failed` and retries; a half-bound gateway is not accepted.
- A `start` envelope delivered on the interactive subject, or an `invoke` on
  the batch one, is poison and receives a terminal acknowledgement.
- The journal is shared and keyed by `command_id`, so a command id can never
  run twice across lanes (the type differs, so the second envelope is a digest
  collision and is terminated; a twin that arrives on the other lane while the
  first copy is still running is terminated at once, not nak'd into a dead
  letter). The journal row has a nullable `lane` column
  (`batch` | `interactive`; NULL for rows written before lanes existed),
  added in place on first start.
- Every lifecycle fact (`turn.started`, `invocation.started/completed/failed`,
  `turn.completed`) carries `data.lane`, so consumers can tell a human-waiting
  turn from batch work. Types and subjects are unchanged.
- The producer rule: use `invoke` only when a human is waiting. n8n, PMs and
  `hookd_bridge` stay on `start`. A producer that cannot see the interactive
  consumer pulling should fall back to `start` and record why (the Holocene
  inbox watcher does, and stamps `lane`/`lane_reason` on `audio.memo.dispatched`).
- Retry horizon: `max_deliver x nak_delay_seconds` is how long a transient
  registry or journal error keeps being retried before the message is abandoned
  in the stream with no consumer action. Interactive is 8 x 3 s (about 21 s);
  batch is 5 x 5 s. A durable's `max_deliver` is part of the bind contract, so
  changing it means updating the live durable (`add_consumer` with the new
  value, a mutable field) before restarting the gateway.
- Each lane's fetch loop is supervised: an unexpected exception is logged
  (`Bloodbank lane consume loop crashed lane=...; resuming`) and the loop
  resumes, so one lane cannot silently stop pulling while the platform still
  reads as connected.
- Alerts: the `nats-exporter` container (monitoring stack) exposes
  `jetstream_consumer_*`; `BloodbankLaneNotPulled` fires when a lane's consumer
  has `num_waiting == 0` and `num_ack_pending == 0` for 4 minutes, and
  `BloodbankInteractiveLaneBacklog` when commands sit queued on the interactive
  lane for 2 minutes.
- Known limits: the interactive lane has 2 slots, FIFO within the lane, and a
  slot is held until the Hermes turn completes or its wall clock expires
  (progress acks keep extending `ack_wait`). Nothing restricts `invoke` to voice
  memos, so any producer that emits it competes for the same two slots; the
  split is by convention. A turn that outlives `turn_timeout_seconds` is
  stopped and its slot freed (next section); a turn whose completion hook never
  fires is now bounded by the same clock instead of holding the slot until the
  gateway restarts.
- Design and rollout: `docs/interactive-command-lane.md`.

## Per-turn wall clock

A turn that never finishes (a hung tool call, a model that loops, a shell
command that sleeps for ten minutes) used to hold its lane slot until the
gateway restarted; two of them blocked every later voice memo. Each lane now has
an optional wall clock for one Hermes turn.

| lane | config | default |
| --- | --- | --- |
| interactive | `extra.interactive.turn_timeout_seconds` | **300** when the key is absent |
| batch | `extra.turn_timeout_seconds` | none (ticket grooming and delegation turns are long on purpose) |

The value is a strict positive number of seconds, at most 86400. A bool, a
numeric string, `null`, zero, a negative, `NaN`/`inf` or a mapping refuses to
start the adapter with `... turn_timeout_seconds must be a positive number ...`;
an absent key takes the lane default. The clock starts when the gateway hands
the turn to Hermes (not while it waits for a same-thread predecessor) and ends
when Hermes reports the turn processed.

**Envelope deadline.** A command may carry `data.deadline_seconds` (a positive
number, on both `invocation.invoke` and `invocation.start`). The effective
budget is `min(deadline_seconds, lane turn_timeout_seconds)`: a command can
shorten the lane limit, never extend it, and a lane with no timeout (batch by
default) ignores it. A non-positive, non-numeric or boolean deadline is poison
and is terminated like any other malformed envelope.

**What happens at expiry**, in this order:

1. The outcome is claimed synchronously (`PendingInvocation.finalizing`), so a
   completion that arrives from now on, including the CANCELLED hook the stop
   itself provokes, is ignored. It cannot publish a second terminal fact or flip
   the outcome.
2. The Hermes turn is **stopped through Hermes' own `/stop` path**: the adapter
   injects an internal `/stop` message for the same session. `BasePlatformAdapter`
   bypasses the active-session guard for it, the gateway runner hard-interrupts
   the running agent (`request_hard_interrupt`), invalidates its run generation,
   reaps the shell processes the turn spawned (so `sleep 120` actually dies) and
   cancels the session's processing task. If the session guard is still held
   afterwards, the adapter calls `cancel_session_processing(session_key)` as a
   fallback. Both are bounded (10 s each).
3. The adapter records whether the session is idle afterwards
   (`turn_stopped`). Hermes can only interrupt a turn at its next
   interruptible point: a tool call blocked in a thread that never checks the
   interrupt flag may keep running after the session is released. In that case
   `turn_stopped` is `false`, the failed fact says "Hermes did not confirm the
   turn stopped", and the slot is **still freed**. That is the one case where a
   timed-out turn may keep spending tokens or tool calls until it unwinds.
4. The journal row is closed: `state = completed`, `outcome = failure`,
   `failure_code = turn_timeout` (a nullable column added in place; an older
   gateway ignores it), with the exact terminal events stored.
5. `bloodbank.agent.invocation.failed` and `bloodbank.conversation.turn.completed`
   (`outcome: failed`) are published, then the JetStream message is **acked**,
   so it is never redelivered. The journal is written before publication: if
   publishing fails the message is NAK'd and the redelivery replays the stored
   events without running the prompt again, like any completed command.

The failed fact:

```json
{"type": "bloodbank.agent.invocation.failed",
 "data": {"error_code": "turn_timeout",
          "error_message": "agent turn timed out after 300s on interactive lane (limit 300s)",
          "lane": "interactive", "elapsed_seconds": 300.2, "timeout_seconds": 300.0,
          "timeout_source": "lane", "turn_stopped": true, "command_id": "...", "...": "..."}}
```

`timeout_source` is `lane` or `envelope` (the command's `deadline_seconds` was
the shorter budget). The slot the turn held is free as soon as the handler
returns, so the next queued interactive command is fetched immediately.

Look it up: `sqlite3 ~/.hermes/bloodbank-hermes-gateway-state.sqlite3
"select command_id, lane, state, outcome, failure_code from executions where
failure_code='turn_timeout'"`, or `mise run memo:trace` for a voice memo.

## Delivery behavior

- Input is capped by `max_command_bytes` (default 256 KiB).
- `max_inflight` (default 4, maximum 64) bounds fetch size, client pending
  messages, JetStream `max_ack_pending`, and active Hermes turns.
- In-progress acknowledgements extend `ack_wait_seconds` during long turns.
- Lifecycle publications use deterministic event IDs and `Nats-Msg-Id`, so
  broker-level retries deduplicate within the stream's duplicate window.
- A mode-`0600` SQLite execution journal persists the command digest, selected
  profile, and exact lifecycle payloads. `pending` means no started-event
  publication has been attempted; `started` is recorded before publication and
  means one or both started events may have escaped. `completed` means the
  Hermes outcome and terminal events were committed before publication.
  Rejection states distinguish an eventless pre-start decision from a
  post-start closure in progress or durably closed.
- If eligibility flips after started publication may have begun, rejection
  intent and a stable timestamp are committed first. The canonical
  `agent.invocation.failed` and `conversation.turn.completed` envelopes are
  then generated deterministically, persisted, and published before `TERM`.
  Persistence or publication failure yields `NAK`; restart/redelivery replays
  the exact stored started-plus-terminal closure with stable event IDs and
  `Nats-Msg-Id`, without invoking Hermes. A pre-start rejection remains
  eventless only when the journal proves publication was never attempted.
- Redelivery of a completed record republishes stored events without invoking
  Hermes. Every durable rejection state is immutable and can never execute,
  even if its route is later re-enabled.
- A turn that exceeds its lane's wall clock is stopped and closed as
  `outcome = failure`, `failure_code = turn_timeout` (see "Per-turn wall clock").
- The command's correlation, causation, command, idempotency, target, thread,
  and turn identifiers are carried into schema-conformant lifecycle events,
  together with the delivery `lane`.

This is not an exactly-once transport claim. JetStream delivery and lifecycle
publication remain at-least-once. The local guarantee is at-most-once Hermes
execution for a command whose `completed` record is durable. A restart while a
record is `pending` or `started` rechecks current routing eligibility before
retrying execution because the completion boundary is unknown; a process crash
after an external side effect but before Hermes' processing-complete callback
can therefore repeat an eligible started command. Do not delete the
execution-state database unless intentionally discarding this deduplication
history.

`multiplex_secondary_adapters: false` is required for the dedicated Bloodbank
gateway topology: profile runtime routing remains enabled, while this process
does not also start each profile's Telegram/Slack adapters and collide with
their separate gateway processes. The option defaults to `true` in Hermes for
backward compatibility.

The plugin publishes only existing canonical events:

- `bloodbank.conversation.turn.started`
- `bloodbank.agent.invocation.started`
- `bloodbank.agent.invocation.completed`
- `bloodbank.agent.invocation.failed`
- `bloodbank.conversation.turn.completed`

## Hermes API assumptions

The implementation relies on the public platform-plugin registration seam,
`BasePlatformAdapter.build_source()`, `MessageEvent.internal`,
`SessionSource.profile`, the processing lifecycle hooks, and, for the per-turn
wall clock, the `/stop` command and `cancel_session_processing()`. Hermes currently
defines `BasePlatformAdapter.handle_message()` as background dispatch; the
adapter therefore waits on `on_processing_complete()` before acknowledging the
JetStream command.

Profile runtime selection requires `gateway.multiplex_profiles: true`. The
dedicated shared Bloodbank process must also set
`gateway.multiplex_secondary_adapters: false`; Telegram and Slack continue in
their existing profile-scoped gateway processes. This plugin does not work
around, replace, or mutate Hermes internals.

## Lifecycle events carry the command context

`bloodbank.agent.invocation.started`, `.completed` and `.failed` echo the
command's `data.context` object verbatim as `data.context` (omitted when the
command carried none). The ticket workflows put `reason`
(`ticket-grooming`/`ticket-delegation`), `repo`, `ticket_key`, `ticket_id`,
`board_id`, `workspace`, `title` and `phase` there, so a consumer can act on a
finished ticket turn statelessly instead of remembering the correlation id.
The `agent.turn.*` events do not carry it.

## Tests

```bash
cd services/hermes-gateway
pytest -q
```
