# Interactive command lane for the Hermes gateway

**Status:** Implemented 2026-10-15 (adapter, schema, watcher, trace, /voice). Written
from a read of the live code and the live broker. Departures from this design are
listed at the end under "As built".

**Problem.** Voice memos and ticket grooming share one JetStream consumer,
`bloodbank-hermes-gateway`, with `max_ack_pending: 4`. A grooming burst of 20+
turns at 5 to 11 minutes each holds all four slots, so a memo that arrives in
the middle waits for the burst (observed: about 14 minutes). The goal is that an
interactive command starts within seconds while the batch lane is saturated.

**Decision in one paragraph.** Add a second command type,
`bloodbank.agent.invocation.invoke` (subject `bloodbank.cmd.agent.invocation.invoke`),
with the same data schema as `invocation.start`. The Hermes gateway adapter binds
a second durable pull consumer to it, `bloodbank-hermes-gateway-interactive`,
with its own two in-flight slots, its own fetch loop and its own task set.
Batch keeps `start` and its four slots. Both lanes dispatch into the same
Hermes runner, which already runs distinct sessions of one profile in parallel,
so no Hermes change is needed. The lifecycle facts are unchanged, so every
existing reader keeps working. The inbox watcher picks the lane, and falls back
to batch (loudly) when nothing is pulling the interactive consumer.

---

## 1. Mechanism

### 1.1 Why not the alternatives

**(b) Same subject, `data.priority`, second consumer: impossible.** A JetStream
consumer selects messages by subject only (`filter_subject` / `filter_subjects`).
It cannot filter on a header or a payload field. The live broker is
nats-server 2.10.29 (`/varz`), which also predates priority groups and pinned
consumers (2.11), and those would rank *clients* inside one consumer anyway, not
messages. Doing priority inside the adapter instead does not work either: a
message can only be inspected after the consumer has delivered it, and a
delivered message counts against `max_ack_pending`. With four batch turns in
flight the broker will not deliver a memo that sits behind them, and holding
extra batch messages un-acked to reach the memo would burn the same slots.
Raising `max_ack_pending` and gating batch client-side leaves the stream FIFO,
so a memo behind 20 queued grooming commands is still behind them.

**(c) A separate stream: unnecessary, and costlier.** Isolation is a property of
the consumer (its `max_ack_pending` and its fetch loop), not of the stream.
`BLOODBANK_COMMANDS` is a `workqueue` stream that owns `bloodbank.cmd.>` and
`bloodbank.rpy.>` (`compose/nats/streams.json`; `docs/event-naming.md` §3). JetStream
forbids two streams with overlapping subjects, so a second command stream means
shrinking the first and rewriting the `cmd -> BLOODBANK_COMMANDS` rule that
every consumer and doc assumes. Two consumers on one workqueue stream are fine
as long as their filters do not overlap, and `...invocation.start` and
`...invocation.invoke` do not.

**(a) Second durable consumer on a distinct subject: chosen.** Smallest change
that gives real isolation, and every piece of it is already supported by the
current contract.

### 1.2 Naming: what the contract allows

`docs/event-naming.md` §3.2 says commands are dispatched by `data.target_agent_id`
and that the agent id is not encoded in the subject. §3.1 says the subject is an
address, so a different delivery class is a different address. §3 binds the
subject to `type` (5 tokens versus 4), so a new subject is a new command type.

The validator (`services/agent-hooks/core/validate.py`) constrains the choice:

- `assert_action_tense` accepts a command action only from `COMMAND_ACTIONS`
  (§8.2). `interactive` is an adjective and is rejected.
- `ALLOWED_ENTITIES` is a global allowlist; adding an entity is a validator edit
  plus a §7 table row.
- Priority, agent ids and thread ids must not appear in `type` (§1, §9).

`invoke` is already in `COMMAND_ACTIONS`, `invocation` is already an entity under
`agent`, and no `agent.invocation.invoke` schema exists. So
`bloodbank.agent.invocation.invoke` needs no validator or allowlist change.
Only a schema file and doc rows are new.

Rejected names: a new entity such as `agent.prompt.send` or `agent.turn.start`
costs a validator change and `agent.turn.start` collides in meaning with the
lifecycle fact `conversation.turn.started`. A token such as `agent.invocation.start.priority`
breaks the 4-token type rule.

**Semantics to write into §3.2:**

| type | subject | meaning |
| --- | --- | --- |
| `bloodbank.agent.invocation.start` | `bloodbank.cmd.agent.invocation.start` | Batch lane. Unattended or scheduled work: ticket grooming, delegation, PM fan-out. FIFO behind other batch work. |
| `bloodbank.agent.invocation.invoke` | `bloodbank.cmd.agent.invocation.invoke` | Interactive lane. A human is waiting on the answer (a voice memo, a chat nudge). Reserved slots. |

`invoke` pairs with the event `invoked` in §8.2 by lexical intent, but the
gateway keeps emitting `agent.invocation.started/completed/failed` for both
commands. §8.2 already says pairing is by schema review, not enforced. Write the
exception in the doc so nobody expects `agent.invocation.invoked`. Keeping the
lifecycle facts identical is the point: Candystore, the n8n Fleet workflow
(consumes `evt.agent.invocation.{started,completed,failed}`), Holocene
`voice-memos.ts` / `fleet.ts` and `clients/hermes.py` need no change.

### 1.3 Schema

New file `schemas/bloodbank/agent/invocation.invoke.json`. It is a copy of
`invocation.start.json` with four constants changed and the data schema
untouched:

- `type`: `bloodbank.agent.invocation.invoke`
- `subject`: `bloodbank.cmd.agent.invocation.invoke`
- `dataschema`: `apicurio://holyfields/bloodbank.agent.invocation.invoke/versions/1`
- `schemaref`: `bloodbank.agent.invocation.invoke.v1`
- `$id` and `title` updated; description says "interactive lane".

Duplicating the `data` block is the least invasive option and `scripts/validate_schemas.sh`
needs nothing else. To stop the two copies drifting, add one test (in
`services/agent-hooks/tests`) asserting
`start.properties.data == invoke.properties.data`. `bb-emit --check --type
bloodbank.cmd.agent.invocation.invoke` then validates end to end, because
`validate_envelope` resolves `schemas/bloodbank/<domain>/<entity>.<action>.json`.

`idempotency_key` is derived by bb-emit as `uuid5(NS, "<type>:<correlationid>")`,
so `start` and `invoke` keys for one memo differ. That matters for the fallback
rule in section 3.2.

### 1.4 Consumer

| | batch (exists) | interactive (new) |
| --- | --- | --- |
| durable | `bloodbank-hermes-gateway` | `bloodbank-hermes-gateway-interactive` |
| `filter_subject` | `bloodbank.cmd.agent.invocation.start` | `bloodbank.cmd.agent.invocation.invoke` |
| `max_ack_pending` = `max_inflight` | 4 (unchanged) | 2 |
| `ack_wait` | 180 s (live config) | 180 s |
| `ack_progress_seconds` | 30 s | 30 s |
| `max_deliver` | 5 | 3 |
| `nak_delay_seconds` | 5 s | 2 s |
| `max_waiting` | 1 | 1 |
| `deliver_policy` / `ack_policy` | all / explicit | all / explicit |

Reasoning: two slots cover one driver talking to two agents with one spare, and
the lane exists to be small. `max_deliver` 3 because a stale voice answer is
worse than a failed one and the watcher already owns retries (`memo:retry`,
revive). `ack_wait` 180 with progress every 30 s matches what the batch lane
actually runs with in `~/.hermes/config.yaml` (the code default of 90/20 is
overridden there); the in-progress ack extends it for long turns. The turn
length cap is the existing `agent.gateway_timeout: 1800` and applies to both.

**Total concurrency becomes 6 turns (4 + 2), not 4.** The gateway runs turn bodies
on an unbounded executor with no session cap (section 2), so this is admission
policy of the adapter, not a Hermes limit. Host has 125 GB RAM with about 62 GB
available, so 2 extra turns are fine.

### 1.5 Adapter changes (`services/hermes-gateway/bloodbank_hermes_gateway`)

Today the single lane is hard-wired in three places: `COMMAND_TYPE` /
`COMMAND_SUBJECT` constants in `contract.py`, `decode_command` which requires
`type == COMMAND_TYPE`, and the adapter's one `_subscription`, `_consumer_task`,
`_inflight_tasks` and `max_inflight`.

Refactor to a small `Lane` value object and run one loop per lane:

```python
# contract.py
@dataclass(frozen=True)
class LaneSpec:
    name: str            # "batch" | "interactive"
    command_type: str    # bloodbank.agent.invocation.start | .invoke
    subject: str         # bloodbank.cmd.agent.invocation.start | .invoke

BATCH = LaneSpec("batch", "bloodbank.agent.invocation.start",
                 "bloodbank.cmd.agent.invocation.start")
INTERACTIVE = LaneSpec("interactive", "bloodbank.agent.invocation.invoke",
                       "bloodbank.cmd.agent.invocation.invoke")
COMMAND_TYPE, COMMAND_SUBJECT = BATCH.command_type, BATCH.subject  # keep imports working

def decode_command(payload, *, max_bytes=262_144, lane: LaneSpec = BATCH): ...
    # type must equal lane.command_type; dataschema/schemaref derive from it
```

```python
# adapter.py
@dataclass
class LaneRuntime:
    spec: LaneSpec
    durable: str
    max_inflight: int
    max_deliver: int
    ack_wait: float
    ack_progress: float
    nak_delay: float
    subscription: Any = None
    task: asyncio.Task | None = None
    inflight: set[asyncio.Task] = field(default_factory=set)
```

- `__init__` builds `self._lanes: list[LaneRuntime]`. Batch is built from the
  existing flat `extra` keys, byte for byte as today. Interactive is built from
  a new `extra.interactive` mapping and exists only when
  `extra.interactive.enabled is true` (strict bool, `_strict_bool`). Absent means
  a single-lane gateway, so the change is backward compatible and the install
  can precede the config edit.
- `connect()` loops over lanes: `pull_subscribe(lane.spec.subject, durable=...,
  stream=COMMAND_STREAM, config=ConsumerConfig(...))`, then `consumer_info` and
  `_assert_consumer_contract(lane, info.config)` (expected `filter_subject`,
  `max_ack_pending`, `max_deliver` are per lane). Then one
  `_consume_loop(lane)` task per lane. If any lane fails to bind, `connect()`
  fails as it does today: the platform reports `bloodbank_connect_failed`
  (retryable). A half-bound gateway is worse than a loudly failed one.
- `_consume_loop(lane)` is the current loop with `lane.inflight` and
  `lane.max_inflight` in place of the instance attributes. The two loops share
  nothing, so a saturated batch lane cannot delay an interactive fetch.
- `_handle_broker_message(message, lane)` passes `lane` to `decode_command` (a
  `start` envelope delivered on the `invoke` subject, or the reverse, is
  `CommandInvalid` and gets `term()`) and uses `lane.nak_delay`. Everything else
  is shared on purpose: `self._records`, `self._execution_claim_lock` (a short
  critical section), the SQLite journal (`command_id` primary key, so a command
  id can never run twice across lanes), `_conversation_locks`, `_publish_many`.
- `disconnect()` cancels every lane's loop and gathers every lane's inflight set.
- Lifecycle events are built from the invocation and do not change. Optionally
  stamp `lane` into the echoed `data.context` for observability. Do not add a
  top-level field to the lifecycle schemas.
- The platform lock (`_acquire_platform_lock("bloodbank-hermes-gateway",
  self.durable_name, ...)`) stays one lock for the one adapter.

Tests to add in `tests/test_adapter.py` and `tests/test_contract.py`: connect
binds two durables with the right config; saturating batch (four never-completing
turns) does not stop an `invoke` command from dispatching; wrong-type-for-lane is
terminal; both lanes write the same journal and a duplicate `command_id` across
lanes is rejected as a digest collision; interactive disabled leaves exactly the
current single-consumer behavior (the existing
`test_connect_binds_one_bounded_durable_pull_consumer` must pass unchanged).

### 1.6 Who creates the consumer

The adapter, on `connect()`. This is how the batch consumer is created today
(`pull_subscribe(..., config=ConsumerConfig(...))` creates it when it is absent,
then `_assert_consumer_contract` refuses a stale one with the wrong filter or
bounds). No ops script, no `nats` CLI (there is none on this host). The
`_assert_consumer_contract` refusal covers the one trap: if somebody later
changes `max_inflight` for the interactive lane, the existing durable no longer
matches and the gateway refuses to bind; the fix is to delete the durable once
(`await js.delete_consumer("BLOODBANK_COMMANDS", "<durable>")` with nats-py) and restart. A workqueue
stream keeps unacknowledged messages, so deleting a durable loses nothing.

### 1.7 Fairness and abuse

- Batch cannot take interactive slots, and interactive cannot take batch slots.
  Each lane's `max_ack_pending` is the hard bound.
- Within the interactive lane it is FIFO. Three memos at once wait for a slot
  behind two running ones. That is acceptable for one operator; raise
  `interactive.max_inflight` if it ever is not.
- The lane is only as good as its discipline. A producer that sends batch work
  on `invoke` defeats it. Rule for §3.2: `invoke` is for commands where a human
  is waiting. n8n (ticket lanes), PMs and `hookd_bridge` stay on `start`. Nothing
  enforces this mechanically, and it is not worth building enforcement for one
  operator.
- No starvation of batch: the two lanes do not compete for slots, only for CPU
  and provider rate limits.

### 1.8 When the interactive consumer is down

Two distinct failures:

1. **Gateway down.** Both lanes are down. Nothing to design: the command waits in
   the stream (workqueue retention, `max_age` 1 day) and the watcher already logs
   `gateway_not_pulling`.
2. **Only the interactive durable is down** (adapter bug in that lane, lane
   disabled in config, durable deleted). The `invoke` subject is still captured
   by `bloodbank.cmd.>`, so a published memo is stored and silently never
   delivered until the consumer returns. A deliberate failure, not a hang, is
   the requirement, so the watcher preflights (section 3.2): when the interactive
   consumer has no puller it publishes the memo on `start` instead and records
   `lane: "batch"` with `lane_reason: "interactive_consumer_not_pulling"` on the
   `dispatched` fact, in the log line and in the trace. The memo is answered
   late, as it is today, and the degradation is visible rather than silent.
   It does not fail the memo, because an answer in 14 minutes beats none.

---

## 2. Hermes-side concurrency: a busy profile does not block a second session

**Question.** `holocene-pm` is mid-grooming-turn. Can the gateway start a second
turn for a different session of the same profile at once, or does Hermes serialize
per profile?

**Answer: it runs them concurrently. The profile is not a concurrency domain.
Only the same session key is serialized.** The lane needs no Hermes change.

Evidence from the installed gateway (`~/.hermes/hermes-agent`, commit
`2bb785c7`, 2026-10-13):

1. **The serialization unit is the session key, not the profile.**
   `gateway/platforms/base.py` `handle_message()` derives `session_key =
   _event_session_key(event)` and checks `if session_key in self._active_sessions`.
   Only a busy key queues a follow-up (`_handle_message_while_active`). Any other
   key goes to `_start_session_processing`, which creates a new asyncio task.
2. **The key includes the thread, so a new `thread_id` is a new key.**
   `gateway/session.py` `build_session_key`: `<agent:profile>:<platform>:<chat_type>:<chat_id>...`.
   The adapter sets `chat_id=invocation.thread_id`, `chat_type="dm"`, and
   `source.profile`. The profile only namespaces the key
   (`_session_key_namespace`). The watcher sends `thread_id = memo_id`, unique per
   memo, so a memo is always a fresh session. Grooming uses per-ticket or
   `bloodbank:<correlationid>` threads.
3. **No admission cap and no pool bound.** `gateway/run.py` `_get_executor()`
   returns an `_UnboundedThreadExecutor`; its docstring says a finite pool
   "silently queued already-accepted turns behind running ones" and that
   `max_concurrent_sessions` "is the only admission cap and is unset by default".
   `hermes_cli/config_defaults.py` has `"max_concurrent_sessions": None` and
   `~/.hermes/config.yaml` does not set it.
4. **The turn lease is per resolved session id.** `gateway/turn_lease.py`
   serializes "the [load history -> run -> flush] region" per session id, to
   protect one transcript. It does not span a profile.
5. **The adapter adds a lock only for the same key.** `_handle_broker_message`
   takes `_conversation_locks[f"{profile}:{thread_id}"]`. Distinct threads of the
   same profile do not contend.
6. **Observed in production.** In `~/.hermes/bloodbank-hermes-gateway-state.sqlite3`
   (570 executions since 2026-08-04), same-profile overlaps with distinct
   `thread_id`s are routine: 66 overlapping pairs since 2026-10-10 on turns under
   30 minutes. Example, profile `green-in-between-pm`: command `64eb764f`
   (436 s) and `f8742cbb` (691 s) overlapped for 399 s, and a third, `c3dacbdb`,
   for 329 s, all on different `bloodbank:<uuid>` threads.

**Residual risks, none of which blocks the lane.**

- Same `thread_id` still serializes (the follow-up is queued as a pending
  message). Interactive producers must not reuse a thread with a running batch
  turn. The watcher's `thread_id = memo_id` guarantees this.
- Cold agent build: a memo for a profile with no cached `AIAgent` pays agent
  construction before the first token. That is seconds, not minutes.
- Provider rate limits and CPU are shared. A 6-turn ceiling is far below the
  unbounded executor's limit.
- `agent_cache_pressure` may evict the LRU cached transcripts under memory
  pressure; it protects the 8 hottest sessions and costs a rebuild, not a block.
- A busy profile's *tools* can contend (the grooming turn holds a checkout or a
  Plane API quota). That is a property of the work, not the gateway.

If the evidence had said "serialized per profile", the fix would have been a
profile-level queue bypass for `invoke` in the adapter. It does not, so the
adapter keeps the plain `handle_message` path.

---

## 3. Producer side and observability

### 3.1 Lane selection

`services/inbox-watcher/inbox_watcher.py` `step_dispatch()` builds the bb-emit
command with a hard-coded `--type bloodbank.cmd.agent.invocation.start`. Change:

```python
LANE = os.environ.get("MEMO_LANE", "interactive")            # interactive | batch
LANE_TYPE = {"interactive": "bloodbank.cmd.agent.invocation.invoke",
             "batch":       "bloodbank.cmd.agent.invocation.start"}
```

and `--type LANE_TYPE[m["lane"]]`, where `m["lane"]` is chosen once per
`command_id` and persisted in the memo state next to `publish_attempted`. bb-emit
already accepts the 5-token subject form (`bloodbank.cmd...`) and `--kind
command`, so no bb-emit change is needed. Run `bb-emit --check --type
bloodbank.cmd.agent.invocation.invoke --data '{...}'` during development.

Nothing else on the wire changes: `--correlation memo_id`, `--command-id`,
`thread_id = memo_id`, `context.reason = "voice-inbox"`.

### 3.2 Preflight, pinning and fallback

Before the first publish attempt for a command id, the watcher probes the lane
consumer through the NATS monitor it already uses (`gateway_liveness()` in
`memo_common.py`, `/jsz?consumers=true&streams=true`).

- Probe `bloodbank-hermes-gateway-interactive` with the existing liveness rule
  (`num_waiting >= 1 or num_ack_pending >= 1`). Because a parked pull request can
  momentarily be absent between fetch cycles, require two failed probes about 1.5 s
  apart before declaring it down.
- Down: set `m["lane"] = "batch"`, `m["lane_reason"] = "interactive_consumer_not_pulling"`,
  log `lane_fallback` at warn, publish on `start`.
- Monitor unreachable (`ok is None`): keep `interactive`. The monitor is
  diagnostic; the publish is core NATS either way.
- **Pin the lane for the life of the `command_id`.** The `idempotency_key`, the
  envelope digest and the journal row are lane-specific, so a retry of the same
  `command_id` on the other subject would be a digest collision (terminal). The
  existing `publish_attempted` path (`dispatch_recovered` via `journal_row(cid)`)
  continues to work because the journal is shared and keyed by `command_id` alone.
  A revived memo (`m.pop("command_id")`) gets a fresh id and may pick a fresh lane.
- Add `lane` and `lane_reason` to the `audio.memo.dispatched` data. The schema
  already has `command_subject` and `additionalProperties: true`; add two
  optional properties to `schemas/bloodbank/audio/memo.dispatched.json` for
  documentation. The lane is also derivable from `command_subject`, so old facts
  remain readable.

The "gateway consumed the command and dropped it" detector in `step_await_agent`
treats `num_pending == 0 and num_ack_pending == 0` as "the consumer is empty".
It must use the memo's lane consumer. With two consumers, reading the batch
consumer for an interactive memo would misreport. `gateway_liveness(lane)` takes
the lane and returns that consumer's numbers.

The deadline extension rule (`ahead = num_pending + num_ack_pending`) becomes
per lane too: an interactive memo is extended only while *its* lane is busy.

### 3.3 `memo:trace`, `memo:list`, `/voice`

- **`memo_common.py`:** `GATEWAY_CONSUMER` becomes a map
  `{lane: durable}`; `gateway_liveness(lane=None)` returns both lanes when called
  with no argument. `gateway_liveness_text` prints
  `interactive: pulling, 1/2 in flight, 0 queued | batch: pulling, 4/4 in flight, 17 queued`.
  The `/2` and `/4` come from the consumer's `config.max_ack_pending`, which the
  monitor returns with `config=true` (the `jsz` call needs `&config=true`).
- **`memo_trace.py`:** the `gateway_journal` hop detail gains `lane=<lane>` and the
  lane's queue state at the time of the trace; the missing-row branch prints the
  memo's lane consumer. `memo:list` gains a LANE column.
- **Watcher heartbeat:** `State.heartbeat()` already writes
  `heartbeat.json` every tick and `apps/api/src/watcher-status.ts` reads it. Add
  `lanes: {interactive: {...}, batch: {...}}` with `waiting`, `ack_pending`,
  `pending`, `max_ack_pending` from the last probe (probe at most every 5 s).
  Holocene stays a sink: it reads a local file the watcher wrote, and the
  per-memo lane comes from the collected `dispatched` fact.
- **`/voice` (`apps/api/src/voice-memos.ts`, `voice-routes.ts`,
  `apps/web/app/voice/voice-app.tsx`):** show a lane chip on each memo, taken
  from `dispatched.data.lane` (fallback: parse `command_subject`). Show a
  banner when `watcher.lanes.interactive` is not pulling ("memos are being sent
  on the batch lane") and a queue depth for the lane the memo is on while it
  waits at "Agent started". `HOPS[agent_started].budgetSeconds` is 120 today;
  with the lane it can stay, and a memo that exceeds it on the interactive lane
  is a real alarm, not queue noise.

### 3.4 Gateway liveness for both consumers

Single source: `GET http://127.0.0.1:8222/jsz?consumers=true&streams=true&config=true`.

```
jq '.account_details[].stream_detail[] | select(.name=="BLOODBANK_COMMANDS")
    | .consumer_detail[] | {name, waiting:.num_waiting, ack_pending:.num_ack_pending,
      pending:.num_pending, max:.config.max_ack_pending, filter:.config.filter_subject}'
```

Expected steady state: both names present, `waiting >= 1` when idle, and the
interactive consumer with `max: 2`, `filter: bloodbank.cmd.agent.invocation.invoke`.
Also check `~/.hermes/gateway_state.json` `.platforms.bloodbank.state == "connected"`
(a lane that fails to bind makes the whole platform not connected). The
`bloodbank-integration` skill's note stands: verify consumption by new
execution rows, not by process liveness. Add the second durable to that skill's
verification text.

---

## 4. Rollout and rollback

The single costly step is restarting `hermes-gateway.service`. It is one
multiplexed process serving about 37 profiles; a restart interrupts every
in-flight turn and takes about two minutes to reconnect every profile. A
restart during a grooming burst is the expensive case: those commands are
redelivered after `ack_wait` and, if their journal row is `started`, re-executed
(README "Delivery behavior"). So restart when `ack_pending` is low.

Order matters: producers must not use `invoke` until the consumer exists. The
watcher's preflight makes the wrong order safe (it falls back to `start`), but do
it in this order anyway.

1. **Bloodbank change (no runtime effect).** On `main` in
   `/home/delorenj/code/33GOD/bloodbank`:
   - add `schemas/bloodbank/agent/invocation.invoke.json` and the data-equality test;
   - extend `docs/event-naming.md` §3.2 with the two-lane table and the
     `invoke`/`invoked` note; add a row for `invoke` under §8.2 usage if useful;
   - add `LaneSpec`, per-lane `decode_command`, `Lane` runtime and
     `extra.interactive` handling to the adapter, plus tests and the README
     "Delivery behavior" paragraph;
   - update `streams.json` `notes` (no structural change) and the
     `bloodbank-integration` skill's gateway notes.
   Verify: `mise run smoketest:schemas`, `cd services/hermes-gateway && pytest -q`,
   `bin/bb-emit --check --type bloodbank.cmd.agent.invocation.invoke --data '{...}'`
   exits 0. Commit and push.
2. **Pre-flight on the live broker.**
   `curl -s 'http://127.0.0.1:8222/jsz?consumers=true&streams=true&config=true'`.
   Note the batch consumer's `num_ack_pending` and `num_pending`. Wait for
   `num_ack_pending <= 1` if the burst can be waited out; if a memo is blocked
   right now, restart anyway (the memo is the thing being fixed).
   The plugin is already symlinked (`~/.hermes/plugins/bloodbank-platform ->
   .../services/hermes-gateway/hermes-plugin`), so the new code is live as soon
   as the process restarts. `install.sh` only needs a re-run if `plugin.yaml`
   dependencies change, and they do not.
3. **Config.** Edit `~/.hermes/config.yaml` under
   `gateway.platforms.bloodbank.extra` (no credentials involved; take a copy to
   `/tmp` first, never beside the file in a repo):
   ```yaml
   extra:
     # existing batch-lane keys unchanged: durable_name, max_inflight: 4,
     # ack_wait_seconds: 180, ack_progress_seconds: 30, ...
     interactive:
       enabled: true
       durable_name: bloodbank-hermes-gateway-interactive
       max_inflight: 2
       max_deliver: 3
       ack_wait_seconds: 180
       ack_progress_seconds: 30
       nak_delay_seconds: 2
   ```
4. **Restart once.** `systemctl --user restart hermes-gateway.service`, then wait
   for reconnection (about 2 minutes):
   - `systemctl --user status hermes-gateway.service` and
     `journalctl --user -u hermes-gateway.service -n 80 | grep -i 'Bloodbank gateway connected'`
     (expect one line per lane);
   - `jq '.platforms.bloodbank.state' ~/.hermes/gateway_state.json` is `"connected"`;
   - the `jsz` query in section 3.4 shows the new durable, `waiting >= 1`, `max: 2`.
5. **Prove isolation before trusting it.** Emit a harmless `invoke` to a
   low-stakes agent and watch a new journal row appear while batch is busy:
   ```bash
   cd /home/delorenj/code/33GOD/bloodbank
   echo '{"target_agent_id":"<agent>","prompt":"Reply with the word ok.","thread_id":"lane-proof-1"}' \
     | bin/bb-emit --strict --print-ids --kind command --type bloodbank.cmd.agent.invocation.invoke \
         --actor-type service --actor-id lane-proof
   sqlite3 ~/.hermes/bloodbank-hermes-gateway-state.sqlite3 \
     "select command_id,state,outcome,created_at from executions order by created_at desc limit 3"
   ```
   The row should appear within seconds even while `start` has four turns running.
   Then a real memo through the Agent Pad.
6. **Switch the producer.** Land the inbox-watcher change in
   `/home/delorenj/code/33GOD/holocene` (lane selection, preflight, lane in
   `dispatched`, per-lane liveness, trace, heartbeat, API/web), run its tests
   (`services/inbox-watcher/test_inbox_watcher.py`) and the Holocene workflow from
   `holocene/CLAUDE.md`: typecheck, `pnpm --filter @holocene/api build`,
   `systemctl --user restart holocene-api.service`, curl `/health`; web:
   `docker compose -f ../33god-platform/compose.yaml -p 33god-platform up -d --no-deps --force-recreate holocene-web`;
   `services/inbox-watcher/install.sh --restart` for the watcher unit.
   Verify `curl -fsSI https://holocene.delo.sh/voice` and one memo end to end with
   `mise run memo:trace latest` showing `lane=interactive`.
7. **Acceptance.** Saturate batch (or wait for a grooming burst), send a memo,
   and require: `agent.invocation.started` within 10 s of `dispatched`, with the
   batch consumer still at `ack_pending: 4`.

### Rollback

Each step reverses independently, cheapest first:

- **Producer only (no gateway restart):** set `MEMO_LANE=batch` in the watcher
  unit and restart `holocene-inbox-watcher.service`. Memos go back to `start`
  immediately. This is the first lever to pull if the lane misbehaves.
- **Disable the lane in the gateway:** set `extra.interactive.enabled: false`
  (or delete the block) and restart `hermes-gateway.service`. The adapter then
  binds only the batch consumer, exactly as before. The interactive durable
  stays on the broker, idle. Any `invoke` messages published afterwards sit in the
  workqueue stream; the watcher falls back to `start` via the preflight.
- **Remove the durable (optional cleanup):** delete
  `bloodbank-hermes-gateway-interactive` with nats-py
  (`await js.delete_consumer("BLOODBANK_COMMANDS", "bloodbank-hermes-gateway-interactive")`)
  after any stranded `invoke` messages are drained or discarded. Stranded
  messages are visible as `num_pending` on that consumer.
- **Revert the code:** `git revert` the adapter commit and restart. Rolling the
  adapter back while `interactive.enabled: true` is still in the config is
  harmless: the old adapter ignores unknown `extra` keys (it reads only the
  keys it knows), so it simply never binds the second durable.

Because the command journal is shared and keyed by `command_id`, no rollback step
can cause a command to run twice that would not already have run twice on a plain
restart.

---

## 5. Open items and non-goals

- **Not changing** the batch lane's four slots, the n8n Fleet node, PMs, or
  `hookd_bridge`. They stay on `start`.
- **Not building** a general priority system. Two lanes, one rule ("a human is
  waiting"). A third lane would be another command type and another consumer, the
  same shape.
- **Candystore** projects events, not commands (`bloodbank-integration` skill), so
  the new command type needs no projection work. Confirm in the first rollout
  that the `invoke` memo's `invocation.*` facts reach `GET /events?correlationid=`.
- **`bb contract`** (`cli/`) prints the legal domains, entities and actions;
  `cli/tests/test_contract_cli.py` already has a `--check` case for
  `bloodbank.cmd.agent.invocation.start` and is the place for the `invoke` twin.
- **Fairness knob if one operator is ever not the user:** `interactive.max_inflight`
  is the only dial; do not add per-agent quotas.

---

## 6. As built (2026-10-15)

- Adapter: `LaneSpec`/`BATCH`/`INTERACTIVE` in `contract.py`; `LaneRuntime` and
  `extra.interactive` in `adapter.py`. The old flat attributes
  (`max_inflight`, `_subscription`, ...) remain as properties over the batch lane.
- Lane recording is both a nullable journal column `lane` (added in place with
  `ALTER TABLE`, `user_version` stays 3, so a code rollback is safe) and
  `data.lane` on every lifecycle fact, not inside the echoed `data.context`
  (context stays a verbatim echo of the command). The three `invocation.*` schemas
  document `data.lane`.
- `ack_wait` and `ack_progress` default to 180 s and 30 s for the interactive lane
  in code (the batch code defaults stay 90/20; live config overrides them).
- Live proof (2026-10-15, after the single gateway restart at 22:09 EDT with `ack_pending` 0):
  with all 4 batch slots busy and 1 queued (`bloodbank-hermes-gateway` `num_ack_pending 4`,
  `num_pending 1`), an `invoke` command to `holocene-pm` had a journal row (`lane=interactive`)
  55 ms after the emit and completed in 7 s, reply `lane ok`. A real voice memo
  (`memo:trace` e2b0b883) dispatched on `invoke` and the agent started 0.02 s later.
- Unrelated finding: `BLOODBANK_COMMANDS` holds 41 old messages on `bloodbank.cmd.audio.task.start`
  that no consumer pulls (workqueue retention, `max_age` 1 d). They are not lane traffic.
- The watcher's `gateway_liveness(lane)` defaults to `batch` (old callers keep their meaning)
  rather than returning both lanes with no argument; `gateway_lanes()` returns both from one
  monitor read.
