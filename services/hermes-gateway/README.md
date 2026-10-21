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
- A turn Hermes did not run is never closed as success (see "Gateway restarts
  and refused turns").
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
- `bloodbank.agent.invocation.progressed` (0..n, see "Progress: interim assistant messages")
- `bloodbank.agent.invocation.completed`
- `bloodbank.agent.invocation.failed`
- `bloodbank.conversation.turn.completed`

## Gateway restarts and refused turns

`systemctl --user reload hermes-gateway.service` (SIGUSR1) and every planned
stop make Hermes drain: the process keeps its adapters connected, waits up to
`restart_after_turn_timeout` (1800 s by default) for in-flight turns, then
gives stragglers `restart_drain_timeout` (60 s here) before interrupting them.
Meanwhile Hermes answers every new message with "⏳ Gateway is restarting and
is not accepting new work right now." through `send()` and reports the turn as
SUCCESS. Before this section existed the plugin journaled that as a completed
command, published `agent.invocation.completed` and acked it: the command was
lost while every consumer believed it ran (journal forensics, 2026-10-21: 29
commands closed as success in under 1 s - 5 on 2026-10-10, 10 on 2026-10-11,
12 on 2026-10-19, 2 voice memos on 2026-10-20 - plus FLUME-37 at 1.24 s and
two 1800 s turn-lease rejections on 2026-10-03).

The plugin reads the runner's `_restart_requested`, `_draining` and
`_external_drain_active` flags (`adapter.gateway_runner`) and:

- **Stops fetching** on every lane while any flag is set. Nothing is pulled,
  so commands stay queued in JetStream and no `max_deliver` budget is spent
  however long the drain lasts; the next process fetches them first. One
  WARNING per lane says `stopped fetching`, one INFO says `resumed fetching`.
- **Checks again right before dispatch** (once before the journal row is
  marked started, once after the started facts are published). A command
  fetched just before the drain is NAKed with the lane's `nak_delay`, its row
  left `pending`/`started` (both dispatch again on redelivery) and nothing
  terminal published.
- **Reads the turn's final reply** (the first non-interim send anchored to the
  command id) against Hermes' own notices (`bloodbank_hermes_gateway/refusals.py`).
  The patterns come from Hermes' i18n catalog (English plus the active
  language, via `agent.i18n`): **every** `gateway.errors.*` key it ships is a
  failure unless it is a fragment (`hint_*`, `processing_incomplete`,
  `*_label`) or listed as not-run, so a new Hermes error notice cannot fall
  through as success. English copies are the fallback when the catalog cannot
  be imported.
  - *not run, retried* (NAK, row stays dispatchable, no completed fact):
    `gateway.busy.drain_rejected_new_work`, `drain_rejected`,
    `draining_maintenance`, `gateway.errors.interrupted_before_start`,
    `previous_turn_cleanup`, `history_unavailable`, and the
    `max_concurrent_sessions` refusal ("Hermes is at the active session limit").
    A false positive here runs the command twice, so a not-run notice must be
    the **whole** reply (a trailing runtime footer of up to 200 characters
    aside); a real answer that merely opens with the same words is an answer.
  - *explicit failure* (`agent.invocation.failed`, acked):
    `error_code = hermes_refused` for `gateway.busy.another_turn_running` (the
    30 min turn-lease wait timed out; a retry would wait again),
    `gateway.unknown_command` (the prompt began with an unknown `/command`) and
    `gateway.busy.drain_queued` (only sent with `busy_input_mode` queue/steer:
    Hermes kept its own copy for after the restart, so a NAK would run it
    twice); `error_code = hermes_turn_error` for every `gateway.errors.*`
    notice in place of an answer (`generic_failed_with_hint`: rate limit, usage
    limit, expired sign-in, quota, overload, rejected request; `rate_limited`,
    `usage_limit_resets`, `provider_kept_failing`, `no_credentials`,
    `auth_failed`, `unreachable`, `connection_interrupted`,
    `connection_unknown`, `bad_request`, `no_response`, `context_overflow`,
    `stopped_before_finishing`, `unexpected_silence`,
    `session_storage_unavailable(_disk)`: persistence failed inside a tool
    round, earlier rounds may have run, so not retried) and for the agent
    loop's own "Operation interrupted ..." abandonment line
    (`agent.operation_interrupted`). All carry `data.refusal` = the key.
- **Checks admission**: Hermes stamps `event._gateway_accepted`; an event its
  ingress drops (unresolved profile route, an internal event while the session
  slot holds a pending message) gets no processing-complete hook, so the batch
  lane used to wait on it forever. It is now NAKed as not run. A prompt that
  starts with `/` and is not admitted was run inline as a bypass slash command
  (`/stop`, `/new`, `/approve` on a busy session), so it is closed at once as
  `hermes_refused` / `not_accepted_slash_command` instead of being redelivered.
- **Never drops on the last delivery**: when a command Hermes did not run is on
  its last JetStream delivery (`num_delivered >= max_deliver`) a NAK would lose
  it silently, so it is closed instead as `error_code = hermes_refused` with
  `data.refusal` = `gateway_restarting` / `gateway_shutting_down` /
  `gateway_external_drain` / `not_accepted` / the notice key.
- **Logs a long pause again**: a lane still paused after 10 min (and every
  10 min after) logs `still not fetching after Ns`, so a drain that never ends
  (a leaked `.drain_request.json`; Hermes itself ignores one older than 1 h) is
  visible.

### Turns the stop drain cuts off

A turn still running when Hermes' `stop()` drain runs out
(`restart_drain_timeout`, 60 s here: every `systemctl --user restart`, and a
reload whose `restart_after_turn_timeout` wait outlasted a long turn) is
hard-interrupted. It then returns its partial text, "Operation interrupted
...", or nothing, which the base adapter reports as SUCCESS; a turn still alive
at adapter teardown is CANCELLED. Hermes' own auto-resume after the restart
skips Bloodbank sessions. Before this, such turns were journaled
completed/success (or cancelled) and acked, and never ran again.

The plugin wraps the runner's `_interrupt_running_agents` (once per runner;
the only caller is the drain-timed-out path) and marks every dispatched command
whose Hermes session it interrupts. Fallbacks when that method is missing:
while `stop()` is under way (`runner._running` false), a CANCELLED turn or one
whose whole reply is "Operation interrupted ..." counts too. A turn that simply
finishes inside the drain window keeps its success.

What happens next is per lane, `shutdown_interrupt` (flat key for batch,
`interactive.shutdown_interrupt` for interactive):

- `fail` (default): closed as `error_code = interrupted_by_shutdown` with
  `data.interrupted_by` = `gateway_restart` / `gateway_shutdown` and
  `data.elapsed_seconds`; partial work may have run, nothing runs twice.
- `retry`: NAKed with the row left `started`, so the next process re-dispatches
  the same thread (its Hermes session holds the partial transcript); tool side
  effects of the cut-off attempt can repeat. On the last delivery it is closed
  as `interrupted_by_shutdown` instead.

Refusal paths that cannot reach a Bloodbank command: pairing/authorization and
the emergency stop (`hermes pause`) skip internal events, as does Hermes'
external-drain gate (the plugin pauses on `_external_drain_active` itself). A
known slash command at the start of a prompt (`/new`, `/status`) on an idle
session is executed by Hermes and still reports success; no production prompt
starts with `/`. Not detectable from the reply: a runner that believes the
session is already running (leaked state after an unconfirmed turn-timeout
stop) steers or queues the prompt into that turn and returns nothing; and a
turn Hermes' own inactivity timeout interrupts mid-answer, outside a shutdown,
whose partial text is not the "Operation interrupted" line.

Find refused commands: `journalctl --user -u hermes-gateway | grep 'not run by
Hermes'`, or `sqlite3 ~/.hermes/bloodbank-hermes-gateway-state.sqlite3 "select
command_id, failure_code from executions where failure_code like 'hermes_%' or
failure_code = 'interrupted_by_shutdown'"`.

## Progress: interim assistant messages

A long turn is mostly real work (10-25 tool rounds for a voice memo). What the
model writes next to its tool calls ("Checking the board first.") is the only
honest account of that work while a human waits, so the gateway publishes each
one as `bloodbank.agent.invocation.progressed`:

- correlationid = the command's; causationid = the turn's `invocation.started`;
  ordering key `turn:<turn_id>`; a random event id (progress is never journaled
  or replayed, and a re-run must not be deduplicated against the first run).
- `data`: `seq` (1..n per command), `text` (the message, trimmed, at most 500
  characters, `text_truncated` says whether it was cut), `elapsed_seconds`
  (since dispatch to Hermes), `command_id`, `target_agent_id`, `lane`,
  `profile_name`, `invocation_id`, `thread_id`, `turn_id`, and the command's
  `context` echoed (a voice memo's carries `memo_id`).
- Every non-blank interim message is published; nothing is throttled at the
  source. Consumers throttle (the Holocene inbox watcher speaks at most one per
  30 s per memo).
- Never for the final reply (that stays on the completion path and is read
  from the Hermes session), never for a blank message, never after the turn
  is finalizing, never for a chat or anchor this adapter is not running.

How the messages are told apart, from Hermes' side of `send()`: Hermes marks
every mid-turn send with `metadata["_interim_send"] = True`, and its stream
consumer anchors assistant commentary to the triggering message
(`metadata["reply_to_message_id"]` = the MessageEvent id = our command id).
Status notices (the "still working" heartbeat, background-review notes,
warnings) are interim but unanchored, tool-progress bubbles and the final reply
carry no interim marker. The adapter declares `SUPPORTS_MESSAGE_EDITING = False`
(it cannot edit anything; `send()` delivers nothing): Hermes then skips token
streaming for Bloodbank turns, so a commentary arrives as ONE whole anchored
send instead of a streamed first chunk plus an unanchored tail. This needs
`display.interim_assistant_messages` on (it is, in `~/.hermes/config.yaml`).
An interim send on a live command's chat that has no anchor is logged at INFO
(`interim send without a command anchor ignored`; the host gateway logs at
WARNING, so raise `bloodbank_hermes_gateway` to INFO to see it): if progress
facts stop appearing after a Hermes update, that line says why. A publish
failure is logged at WARNING and never fails the send or the turn.

Whether a turn has any progress at all is up to the model: a tool call with no
text next to it is not a message. Measured 2026-10-20 on the last three days of
memo turns, 0-4 of 8-18 tool rounds carried text, so the Holocene watcher's
voice-memo prompt asks for one short sentence before each tool call. Proof run
(holocene-pm, interactive, "before each tool call, write one short sentence...
check the date, then count the files in /tmp"): `progressed` seq 1 at 12.8 s
("I'll check the current date with the shell."), seq 2 at 20.4 s, then
`completed`.

```bash
curl -s 'http://127.0.0.1:8683/events?correlationid=<id>&type=bloodbank.agent.invocation.progressed&limit=50'
journalctl --user -u hermes-gateway.service | grep 'Bloodbank progress published'
```

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
