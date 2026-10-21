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
| `max_deliver` | 5 | 8 (was 3: a ~6 s horizon abandoned a memo on a transient registry rewrite) |
| `nak_delay_seconds` | 5 s | 3 s |
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
       max_deliver: 8
       ack_wait_seconds: 180
       ack_progress_seconds: 30
       nak_delay_seconds: 3
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

## Review round 2 (2026-10-15)

Fixed after review of the as-built lane:

- Fallback no longer sends a memo to batch when the whole gateway is down. The
  watcher reads both lanes in one monitor call and falls back only when
  interactive has no puller and batch does. With both unpulled (a gateway
  restart) the memo stays on `invoke` and starts first on reconnect
  (`lane_reason=gateway_not_pulling_queued_on_interactive`).
- A concurrent same-`command_id` twin on the other lane (or with another
  envelope) is terminated instead of nak'd.
- Interactive `max_deliver` 3 -> 8 and `nak_delay` 2 -> 3 s: the retry horizon was
  about 6 s. The live durable was updated in place before the gateway restart.
- Each lane's fetch loop is supervised, and Prometheus alerts exist for a lane
  losing its puller or building a backlog (monitoring stack, `nats-exporter`).
- Known limit: 2 interactive slots; `invoke` is restricted to voice memos by
  convention only. The missing per-turn wall clock was added the next day (section 7).

---

## 7. Per-turn wall clock (2026-10-16)

### 7.1 Problem

A slot is held until Hermes reports the turn processed, and progress acks keep
the message alive indefinitely. Two long or hung voice turns (a model loop, a
shell command that never returns) therefore held both interactive slots and
blocked every later memo until the gateway was restarted, which costs about two
minutes of reconnect and interrupts every other in-flight turn.

### 7.2 Decision

A wall clock per Hermes turn, enforced by the adapter, per lane:

- `extra.interactive.turn_timeout_seconds`, default **300** (a human is waiting;
  five minutes is already a failure from their side). Strict positive number,
  at most 86400.
- `extra.turn_timeout_seconds` for the batch lane, **absent = no timeout**
  (grooming and delegation turns are long on purpose). The option is lane-generic
  (`LaneRuntime.turn_timeout`), so enabling it for batch is a config change.
- `data.deadline_seconds` on the command (both command schemas, which must keep
  an identical `data` payload): effective budget is `min(deadline, lane limit)`.
  It can only shorten the lane limit; a lane with no limit ignores it. It exists
  for callers that know a task is short, and it makes the live proof possible
  without touching the gateway config.

Rejected: ending the wait without stopping the turn (frees the NATS slot but the
agent keeps spending tokens and tool calls, and the next memo then competes with
a ghost); a timeout only in the Holocene watcher (the watcher cannot stop a turn
and the other `invoke` producers would stay unbounded); lowering `ack_wait` to
let JetStream redeliver (it would re-run the prompt, the opposite of the goal).

### 7.3 How a turn is stopped

Read from the Hermes gateway source in use
(`~/.hermes/hermes-agent/gateway/platforms/base.py`, `slash_commands.py`,
`run_agent_cache.py`). `/stop` is the supported path and it is what a user
typing `/stop` in a chat gets:

1. `BasePlatformAdapter.handle_message` sees a command that bypasses the
   active-session guard (`should_bypass_active_session`) and calls
   `_dispatch_active_session_command`.
2. The runner's `_handle_stop_command` -> `_interrupt_and_clear_session` ->
   `_interrupt_running_turn`: `request_hard_interrupt(running_agent)`, bump the
   session's run generation (so a late result is discarded), and start a reaper
   that kills the shell processes the turn spawned (`sleep 120` dies, it is not
   merely abandoned). It also interrupts detached delegations of that session.
3. `cancel_session_processing(session_key)` cancels the session's asyncio task.
   The cancelled task's `on_processing_complete(CANCELLED)` hook fires.

The adapter injects an internal `/stop` `MessageEvent` for the same source
(profile, `chat_id = thread_id`), so it needs no private runner API. Afterwards
it checks the session guard; if Hermes still holds it, it calls
`cancel_session_processing` itself. `turn_stopped` records whether the session
was idle at the end.

What Hermes cannot do: interrupt a tool call that is blocked inside a thread
that never polls its interrupt flag. The session is released and the task is
cancelled, but that thread can outlive them. The adapter then publishes
`turn_stopped: false` and the failed fact says so; the slot is freed anyway, so
the failure is bounded to one stray tool call, not a stuck lane.

### 7.4 Sequence at expiry

1. Claim the outcome synchronously (`PendingInvocation.finalizing = true`). The
   Hermes completion hook, including the CANCELLED one our own stop causes, returns
   early when the flag is set; and a completion that wins the race first makes the
   timeout stand down. There is exactly one terminal outcome per command.
2. Stop the turn (7.3), bounded to 10 s per step.
3. `ExecutionStateStore.mark_completed(outcome="failure", failure_code="turn_timeout")`
   with the stored terminal events. Journal before publish, so a failed publish
   replays the identical events on redelivery and never re-runs the prompt.
4. Publish `agent.invocation.failed` (`error_code turn_timeout`, plus
   `elapsed_seconds`, `timeout_seconds`, `timeout_source`, `turn_stopped`) and
   `conversation.turn.completed` (`outcome failed`).
5. Release the conversation lock, ack the message (no redelivery), free the slot
   (the lane's fetch loop wakes as the in-flight task ends).

The journal keeps `state='completed'`, `outcome='failure'` and adds a nullable
`failure_code` column. A new `outcome='timeout'` would have needed a table
rebuild because the `CHECK` on `outcome` is baked into existing databases, and a
rebuild is a one-way schema bump that a code rollback cannot read.
`failure_code` is added in place like `lane` (`user_version` stays 3).

### 7.5 Holocene

- Inbox watcher: reads `failure_code` from the journal (and the `error_code` on
  the failed fact from Candystore) and fails the memo
  `failed(stage=agent_reply, error_code=turn_timeout, retryable=false)` with
  `lane`, `elapsed_seconds`, `timeout_seconds`, `timeout_source`, `turn_stopped`.
  `memo:retry` re-dispatches under a fresh command id.
- `memo:trace` verdict: `STOPPED AT agent finished: turn timeout - <message>`.
- API/`/voice`: stage label `Agent turn timed out after Ns`; the **Agent
  finished** hop carries the code, message and a `turn stopped` meta line.

### 7.6 Tests

`services/hermes-gateway/tests/test_adapter.py` (`make_timeout_adapter`): the
timeout fires, `/stop` is invoked and the session is idle, journal and lifecycle
facts are correct, the message is acked once with no NAK or redelivery, a third
interactive command runs in the freed slot, a late or racing completion is
ignored, an unconfirmed stop is reported as such, `deadline_seconds` shortens but
never extends, batch has no clock by default, a replayed timed-out command never
re-runs, a failed terminal publish NAKs then replays, and the config is strict.
The conftest fake models Hermes' `/stop` and `cancel_session_processing`.

### 7.7 Live proof (2026-10-16)

Single gateway restart at 16:39 UTC with both consumers at `ack_pending 0`;
`turn_timeout_seconds: 300` set under `interactive`. Both durables pulling and
`gateway_state` bloodbank `connected` afterwards. Production value stays 300; no
second restart was needed because the proof used `data.deadline_seconds`.

- Hung turn: `invoke` to `holocene-pm` with prompt "Run the shell command: sleep 120;
  then reply with the single word done" and `deadline_seconds: 20`. 20.1 s later the
  journal row read `completed / failure / turn_timeout`, `invocation.failed` carried
  `turn_timeout`, `elapsed_seconds 20.1`, `timeout_source envelope`, `turn_stopped true`, and the gateway
  logged `Bloodbank turn timed out lane=interactive ... stopped=True`. No `sleep 120` process
  remained and the Hermes session ended with no "done" (Hermes recorded only its own
  "request was not processed" notice).
- Slot freed: two such hung turns (25 s deadline) plus a smoke command emitted 2 s
  later on the 2-slot lane. The smoke command's journal row was created 37 ms after the first
  slot closed (16:45:08.36 -> 16:45:08.40) and it completed `success`, where before
  this change it would have waited for a gateway restart.
- Normal turn: an `invoke` smoke command completed `success` in 11 s.
- Real memo, production value (300 s, no envelope deadline): a synthesized voice memo
  ("Run the shell command sleep 1000 in the foreground, wait for it to finish, and then reply
  done") uploaded to `inbox/holocene-pm/2026/10/16/`. The agent started `sleep 1000`; at 300.1 s the
  journal row closed `completed / failure / turn_timeout`, the Hermes session shows the process
  killed (`process_manage` exit -15, `termination_source: gateway_turn_interrupt`) and no "done"
  reply, the watcher published `audio.memo.failed(agent_reply, turn_timeout, retryable=false)`,
  `memo:trace` ended `STOPPED AT agent finished: turn timeout - agent turn timed out after 300.1s
  on interactive lane (limit 300s)`, and `GET /api/modules/voice/memos/<id>` reports stage label
  `Agent turn timed out after 300s`.

## 8. Reload drain

### 8.1 Behaviour

`systemctl --user reload hermes-gateway.service` sends SIGUSR1. Hermes sets
`_restart_requested`, writes `gateway_state: "draining"`, and waits for in-flight
turns (up to `restart_after_turn_timeout`, 1800 s by default) before it exits 75
and systemd relaunches it. During that wait Hermes answers every new message
with "Gateway is restarting and is not accepting new work right now" and
reports the turn as SUCCESS. Before `e02c7cf` the adapter journaled that as
`completed/success`, published `agent.invocation.completed` and acked it, so
the command was lost (29 rows in the journal closed `success` in under 1 s
from 2026-10-10 to 2026-10-20, plus a few slower ones going back to 2026-10-01).

Since `e02c7cf` (`adapter.py`, `refusals.py`):

- While `_restart_requested`, `_draining` or `_external_drain_active` is set,
  neither lane fetches. Commands stay in JetStream with no delivery, so a drain
  of any length spends none of `max_deliver`. Each lane logs one WARNING
  `Bloodbank lane=<lane> stopped fetching: ...` when it pauses.
- A command fetched before the flag flipped is NAKed with a delay just before
  dispatch. Its journal row stays dispatchable and nothing terminal is
  published.
- A turn whose first final reply is a Hermes refusal notice is not success:
  drain and busy refusals NAK as not run, and error notices close as
  `hermes_refused` / `hermes_turn_error`.
- In-flight turns keep running and close normally during the drain.

Since the follow-up review fix (see the gateway README, "Turns the stop drain
cuts off"):

- A turn still running when the stop drain runs out (`restart_drain_timeout`,
  or a reload whose 30 min wait outlasted it) is hard-interrupted by Hermes and
  used to close as `success` with its partial text (POOL-12/13/14 on
  2026-10-11, NAVI-41 on 2026-10-19, skillex-pm's 2 h 16 min repair turn on
  2026-10-20) or as `cancelled` (HOLOC-13 grooming on 2026-10-11). The plugin
  wraps the runner's `_interrupt_running_agents` and closes such a turn as
  `interrupted_by_shutdown` (`data.interrupted_by`, `data.elapsed_seconds`), or
  NAKs it for the next process with `shutdown_interrupt: retry` on that lane.
- Every `gateway.errors.*` notice in Hermes' catalog is classified (not just a
  hand-picked list); a not-run notice must be the whole reply, so an answer
  that opens with the same words is never re-run; `drain_queued` (Hermes kept
  its own copy) closes as `hermes_refused` instead of a NAK.

### 8.2 Live proof (2026-10-21)

Three harmless commands to `holocene-pm`, each with its own thread, emitted with
`bin/bb-emit --strict --print-ids --kind command` (actor `drain-proof`). Before
starting, both consumers were at `ack_pending 0`, `active_agents 0`,
`gateway_state running`.

| # | lane | command_id | thread_id | prompt |
| --- | --- | --- | --- | --- |
| 1 | interactive (`invoke`) | `facaedd1-5937-41e5-a8b5-b6a338860f13` | `drain-proof-20261021-1-5c2f6960` | "Drain test 1: run the shell command sleep 80, then reply with the single line 'drain test 1 done'. Take no other action." |
| 2 | interactive (`invoke`) | `06290f62-958c-4d0a-9410-e85392e8f23c` | `drain-proof-20261021-2-377cacda` | "Drain test 2: reply with the single line 'drain test 2 done'. Take no other action." |
| 3 | batch (`start`) | `bbc2ba6f-fcf9-4418-be6a-2b053274f1c5` | `drain-proof-20261021-3-121c2245` | "Drain test 3: reply with the single line 'drain test 3 done'. Take no other action." |

Correlation ids: 1 `5c2f6960-caea-448b-84f9-fe06020f5c9f`, 2
`377cacda-facd-4cbc-9267-769cb1fab23f`, 3 `121c2245-e3c7-4e9c-bdf6-c9c66c010aa8`.

Timeline (UTC). Hermes logs are local time (EDT, UTC-4) and are converted here.

| time | event |
| --- | --- |
| 01:00:12.20 | command 1 emitted |
| 01:00:12.27 | command 1 journal row `started`, interactive `ack_pending 1` |
| 01:00:19 | holocene-pm calls `terminal` with `sleep 80` (foreground) |
| 01:00:32.22 | `systemctl --user reload hermes-gateway.service` |
| 01:00:32.25 | `SIGUSR1 received ... drain active turns, exit, supervisor relaunches`; `Restart deferred: waiting on 1 active work unit(s) ... 1800s remaining` |
| 01:00:32.76 | `gateway_state.json`: `draining`, `restart_requested true` |
| 01:00:32.84 | `Bloodbank lane=batch stopped fetching: Hermes gateway is restarting (draining in-flight turns) ...` |
| 01:00:33.24 | same for `lane=interactive` |
| 01:00:35 | both consumers `num_waiting 0` (the last pull request expired, no new one was issued) |
| 01:00:40.26 | command 2 emitted (interactive) |
| 01:00:40.30 | command 3 emitted (batch) |
| 01:00:44 to 01:01:53 | bloodbank still connected, gateway draining: interactive `waiting 0, pending 1, ack_pending 1` (command 1), batch `waiting 0, pending 1, ack_pending 0`. No journal row for 2 or 3. Candystore has no fact for correlation 2 or 3. |
| 01:01:40 | `sleep 80` returns exit 0 |
| 01:01:42 | holocene-pm replies "drain test 1 done" |
| 01:01:42.86 | Hermes: `Stopping gateway for restart...` |
| 01:01:43.02 | command 1 journal `completed/success` (90.75 s), `agent.invocation.completed` published, acked (interactive `ack_pending 0` by 01:01:49) |
| 01:01:53.18 | `bloodbank disconnected` |
| 01:02:19 | old process exits, systemd `Scheduled restart job` |
| 01:02:24 | new process (pid 898594) started |
| 01:03:32.57 | `Bloodbank gateway connected lane=batch` / `lane=interactive` |
| 01:03:32.64 | command 2 journal row `started` (65 ms after connect) |
| 01:03:33.17 | command 3 journal row `started` |
| 01:03:39.35 | command 2 `completed/success` (6.71 s) |
| 01:03:41.98 | command 3 `completed/success` (8.81 s) |
| 01:06:51 | `Gateway running with 18 platform(s)` (the other profiles finish starting) |

Proof points:

- **Neither lane pulled during the drain.** Both consumers sat at `num_waiting 0`
  with commands 2 and 3 in `num_pending` for the whole drain, while the
  Bloodbank platform was still connected. The adapter logged no NAK and no
  "not run" line: the commands were never fetched.
- **No false closure.** No journal row existed for 2 or 3 until the new process,
  and Candystore shows no `agent.invocation.*` fact for either before 01:03:32.
  Each has exactly one `invocation.started` and one `invocation.completed`, both
  from the new process.
- **The in-flight turn finished during the drain.** The command 1 Hermes session
  holds the `sleep 80` tool call (exit 0) and the reply "drain test 1 done". The
  journal row closed `success` at 90.75 s and was acked before the adapter
  disconnected. Hermes began `Stopping gateway for restart` 0.16 s before the
  adapter journaled it, and the adapter disconnected 10 s later.
- **Commands 2 and 3 ran for real after the restart.** The holocene-pm
  `state.db` sessions for their threads hold the user prompt and the assistant
  replies "drain test 2 done" (01:03:39.15) and "drain test 3 done"
  (01:03:41.89). These are real agent turns of 6.7 s and 8.8 s, not a notice
  answered in 0.1 s.
- **One delivery each.** Interactive `delivered.consumer_seq` went 70 -> 71
  (command 1) -> 72 (command 2), and batch went 813 -> 814 (command 3).
  `num_redelivered` stayed 0 on both consumers throughout, so no `max_deliver`
  budget was spent.
- **After the restart** both lanes were at `num_waiting 1`, `ack_pending 0`;
  `gateway_state` `running`; bloodbank `connected` (pid 898594).

Holocene during the drain (01:01:02):

- `memo_common.gateway_liveness_text` (what `memo:trace` prints):
  `interactive: gateway restarting (draining), memo queued; it runs when the gateway is back (waiting on 2 in-flight turns), 1/2 in flight finishing; consumer bloodbank-hermes-gateway-interactive: waiting=0 pending=1 ack_pending=1`
  and `batch: gateway restarting (draining), memo queued; it runs when the gateway is back (waiting on 2 in-flight turns); consumer bloodbank-hermes-gateway: waiting=0 pending=1 ack_pending=0`.
- `gateway_lanes_text`: `interactive: gateway restarting (draining), 1/2 in flight, 1 queued | batch: gateway restarting (draining), 0/4 in flight, 1 queued`.
- The watcher heartbeat and `GET /api/modules/voice/memos` `.watcher.lanes`
  report `draining: true` with that `gateway` sentence on both lanes. Batch shows
  `pulling: false`, but because of the drain flag the banner reads "restarting",
  not "NOBODY PULLING".
- "2 in-flight turns" is Hermes' `active_agents`: command 1 plus an external cron
  job (`777c511a009c`) that Hermes listed as restart-safe and excluded from the
  wait.

Cost of this reload for queued work: commands emitted at 01:00:40 started at
01:03:32 (2 min 52 s). That breaks down as 62 s waiting for command 1, about
37 s old-process shutdown, 5 s `RestartSec`, and 68 s for the new process to
reach the Bloodbank adapter. The Bloodbank lanes run about 3 min 20 s before the
whole gateway reports `running`.

Sampler and raw log: `/tmp/claude-drainproof/observe.py`, `observe.log` (one
JSON line per change, consumers, gateway state, journal rows and Candystore fact
types).

### 8.3 Live proof, second run (2026-10-21, plugin `5853150`, Holocene `dcbcfa9`)

The same three prompts, re-run against the follow-up fix: the stop-drain wrap
around `_interrupt_running_agents` (`5853150`) and the Holocene wording for
restarting, stopping, maintenance and starting (`dcbcfa9`). The plugin is the
bloodbank checkout (`~/.hermes/plugins/bloodbank-platform` is a symlink), clean
at `5853150`. Before starting (02:07:45), both consumers were at `ack_pending 0`
and `num_waiting 1`, `active_agents 0`, `gateway_state running` (pid 2180084,
started 01:55:51 on that code). Emitted with `bin/bb-emit --strict --print-ids
--kind command --actor-type operator --actor-id drain-proof`.

This run adds two checks to 8.2:

- A turn that ends inside the stop window must keep its `success`. The new
  wrap must not mark it `interrupted_by_shutdown`.
- The wording Holocene shows between the old and new process, and while the new
  process starts.

| # | lane | command_id | thread_id | correlation |
| --- | --- | --- | --- | --- |
| 1 | interactive (`invoke`) | `978c9425-d032-46a1-b14e-b52ebdcf560e` | `drain-proof2-20261021-1-7b49d577` | `7b49d577-0052-44f8-8bc2-6019866191e3` |
| 2 | interactive (`invoke`) | `369fca44-af3c-4dad-b765-01dd5994ccae` | `drain-proof2-20261021-2-59525076` | `59525076-d04d-4136-90fc-0b4d5291fdc9` |
| 3 | batch (`start`) | `c5662f18-4faf-4679-ae25-df2eca3606d5` | `drain-proof2-20261021-3-57975d64` | `57975d64-3932-433e-8c72-4a69b111ae17` |

The prompts match 8.2: command 1 asks for `sleep 80`, then "drain test 1 done".
Commands 2 and 3 ask for "drain test N done" only.

Timeline (UTC; Hermes local-time log lines converted):

| time | event |
| --- | --- |
| 02:07:48.85 | command 1 emitted |
| 02:07:48.87 | command 1 journal row `started`; `invocation.started` published |
| 02:07:56.84 | holocene-pm calls `terminal` with `sleep 80` (foreground, timeout 120) |
| 02:08:22.03 | `systemctl --user reload hermes-gateway.service`. Hermes logs `SIGUSR1 received ...` and `Restart deferred: waiting on 1 active work unit(s) ... 1800s remaining` (command 1's session, 33.1 s in, `current_tool: terminal`) |
| 02:08:22.05 | `gateway_state.json`: `draining`, `restart_requested true`, `active_agents 1` |
| 02:08:22.90 | `Bloodbank lane=batch stopped fetching: ...` and `lane=interactive stopped fetching: ...` (same millisecond) |
| 02:08:24.09 | both consumers at `num_waiting 0` (interactive `ack_pending 1` is command 1) |
| 02:08:24.13 | command 2 emitted (interactive) |
| 02:08:24.19 | command 3 emitted (batch); both consumers now `waiting 0, pending 1` |
| 02:08:24 to 02:11:08 | Neither consumer pulls (`num_waiting 0`) and commands 2 and 3 stay in `num_pending`. There is no journal row and no Candystore fact for either. The old process stayed connected to Bloodbank until 02:09:30. |
| 02:09:17.25 | `sleep 80` returns exit 0 |
| 02:09:19.41 | holocene-pm replies "drain test 1 done" |
| 02:09:19.88 | `Restart deferred wait complete — active work drained; proceeding to stop()` |
| 02:09:19.96 | `Stopping gateway for restart...`; `[Bloodbank] Sending response (17 chars)` to thread 1 |
| 02:09:20.12 | command 1 journal `completed/success` (91.25 s); `agent.invocation.completed` published; acked (interactive `ack_pending 0` by 02:09:24.7) |
| 02:09:27.30 | `Shutdown phase: drain done ... timed_out=False, active_at_start=0`: nothing left for `_interrupt_running_agents` |
| 02:09:30.89 | `bloodbank disconnected` |
| 02:09:53.68 | `Gateway stopped (total teardown 33.72s)`; `gateway_state.json`: `stopped`, `restart_requested true` |
| 02:10:01 | systemd `Scheduled restart job`; new process pid 2505566 (by 02:10:12 `gateway_state.json` carries the new pid, still `stopped`, `restart_requested true`) |
| 02:11:04.56 | `Starting Hermes Gateway...`; by 02:11:06.8 the new process writes `starting` with `restart_requested true` |
| 02:11:08.39 | `Bloodbank gateway connected lane=batch` and `lane=interactive` |
| 02:11:08.49 | command 3 journal row created (`pending`, then `started`), 100 ms after connect |
| 02:11:09.33 | command 2 journal row `started` |
| 02:11:14.85 | command 3 `completed/success` (6.36 s) |
| 02:11:15.04 | command 2 `completed/success` (5.71 s) |
| 02:14:28.59 | `gateway_state running`, `restart_requested false` (the other profiles have finished starting) |

Proof points:

- **Neither lane pulled during the drain.** Both consumers stayed at
  `num_waiting 0`, with commands 2 and 3 in `num_pending`, from 02:08:24 until
  the new process connected at 02:11:08. The adapter logged no NAK and no "not
  run" line. Between 22:08 and 22:15 local, Hermes sent only three Bloodbank
  responses, all 17 characters long: command 1 during the drain, then commands
  2 and 3 in the new process. No 64- or 67-character "not accepting new work"
  notice was sent.
- **No false closure.** The journal had no row for 2 or 3 until 02:11:08, and
  Candystore had no fact for their correlations until 02:11:08.49. Each now has
  one `conversation.turn.started`, one `invocation.started`, one
  `invocation.completed` and one `conversation.turn.completed`, all from the
  new process.
- **The in-flight turn finished during the drain and kept its success under
  `5853150`.** Command 1 closed `success` 0.16 s after `stop()` began, the same
  case as 8.2. Hermes' stop drain found `active_at_start=0`, so the
  `_interrupt_running_agents` wrap marked nothing. The fallbacks did not fire
  either (a CANCELLED turn during `stop()`, or a reply that is only "Operation
  interrupted ..."). The row has no `failure_code`.
- **Commands 2 and 3 ran for real after the restart.** In holocene-pm's
  `state.db`, session `20261020_221109_2de8aa72` (thread 2) and session
  `20261020_221109_9ca4c302` (thread 3) hold the user prompt and the replies
  "drain test 2 done" (02:11:14.91) and "drain test 3 done" (02:11:14.70).
  Command 1's session `20261020_220748_6b94259f` holds the `sleep 80` call,
  `exit_code 0` and "drain test 1 done".
- **One delivery each.** Interactive `delivered.consumer_seq` went 72 -> 73
  (command 1) -> 74 (command 2), and batch went 814 -> 815 (command 3).
  `num_redelivered` stayed 0 on both consumers throughout.
- **After the restart** both lanes were at `num_waiting 1` and `ack_pending 0`,
  bloodbank `connected` (pid 2505566), and `gateway_state running` from
  02:14:28.

Holocene through the cycle, sampled every 2 s from `memo_common` (what
`memo:trace` prints) and `GET /api/modules/voice/memos` `.watcher.lanes`:

- **Drain** (02:08:23 to 02:09:53). Per-lane trace lines:
  - `interactive: gateway restarting (draining), memo queued; it runs when the gateway is back (waiting on 1 in-flight turn), 1/2 in flight finishing; consumer bloodbank-hermes-gateway-interactive: waiting=0 pending=1 ack_pending=1`
  - `batch: gateway restarting (draining), memo queued; it runs when the gateway is back (waiting on 1 in-flight turn); consumer bloodbank-hermes-gateway: waiting=0 pending=1 ack_pending=0`
  - Lanes line: `interactive: gateway restarting (draining), 1/2 in flight, 1 queued | batch: gateway restarting (draining), 0/4 in flight, 1 queued`.
  - From 02:08:27 the API lanes carry `draining: true` and `gatewayLabel` "gateway restarting (draining)". Batch is `pulling: false`, and the banner says "restarting", not "NOBODY PULLING".
- **Between processes** (02:09:54 to 02:11:06; `stopped` with
  `restart_requested true`). Both lanes read
  `gateway restarting, memo queued; it runs when the gateway is back`, and so
  does the API from 02:09:56 (the `restarting` kind, present since `120e2a5`).
- **New process starting** (02:11:06 to 02:14:28). The wording was wrong here
  (fixed in Holocene `85a63a3`; see 8.4).
  - The new process writes `gateway_state: starting` with
    `restart_requested: true`.
  - `memo_common.gateway_state` tests `restarting` first (`memo_common.py:652`:
    a fresh `draining`, `stopped` or `starting` state with
    `restart_requested`), so the kind is `restarting`, not `starting`.
  - The exemption "a starting process whose lanes already pull is just pulling"
    in `gateway_lanes` checks `gw["starting"]`, so it never fires.
  - The lanes connected at 02:11:08 and ran commands 2 and 3. For the next
    3 min 20 s, `memo:trace`, the watcher and the API still said "gateway
    restarting, memo queued; it runs when the gateway is back". At 02:11:08 the
    trace even printed "1/2 in flight finishing" for turns running in the new
    process.
  - Likely fix: when the writer pid is alive and the state is `starting`,
    classify it as `starting` whatever `restart_requested` says. Then lanes that
    already pull read as pulling.
- **Running** (02:14:30): `interactive: pulling, 0/2 in flight, 0 queued | batch: pulling, 0/4 in flight, 0 queued`.

Cost of this reload for queued work: commands emitted at 02:08:24 started at
02:11:08.5 (2 min 44 s):

- 55.7 s waiting for command 1
- 33.7 s old-process teardown
- about 7 s for the process exit and the 5 s `RestartSec`
- 67 s for the new process to reach the Bloodbank adapter (the 1Password
  secret reads, then `Starting Hermes Gateway` at +63 s)

As in 8.2, the Bloodbank lanes run for about 3 min 20 s before the whole gateway
reports `running`.

Raw logs are in `/tmp/claude-drainproof2/`:

- `driver.sh` and `driver.log`: the scripted steps with timestamps.
- `observe.log`: consumers, gateway state, journal rows and Candystore fact
  types, one JSON line per change.
- `holo_sample.py` and `holo.log`: the Holocene wording and API lanes, one JSON
  line per change.

### 8.4 Live proof: a short drain and `shutdown_interrupt: retry` (2026-10-21)

Config change under test (`~/.hermes/config.yaml`, hand-maintained): `agent.restart_after_turn_timeout: 120`
(default 1800) and `shutdown_interrupt: retry` on both lanes
(`gateway.platforms.bloodbank.extra` and its `interactive` block). A reload now
holds commands at most about 3 minutes, and a turn the stop drain cuts off is
NAKed and runs again on the same conversation. Gateway plugin `5853150`, Holocene
`85a63a3`. Two long turns to `holocene-pm`, emitted with `bin/bb-emit --strict
--print-ids --kind command --actor-type operator --actor-id retry-proof`, then
`systemctl --user reload hermes-gateway.service`. Before each run both consumers
were at `ack_pending 0`, `num_waiting 1`, `active_agents 0`, `gateway_state
running`.

| # | lane | prompt |
| --- | --- | --- |
| 1 | batch (`start`) | "Retry test: run the shell command sleep 240, then reply with the single line 'retry test done'. Take no other action." |
| 2 | interactive (`invoke`) | "Retry test 2: run sleep 200, then reply 'retry test 2 done'." |

#### First attempt: reload 68 s into the turns (no cut)

Commands `d17f19c8-49f6-462f-83df-5abed7a7753e` (1) and
`30f87bd3-b648-4752-9396-2a6aa439fd87` (2), threads
`retry-proof-20261021-{1-c84e9161,2-bebae3b5}`. The driver waited 60 s for the
`sleep` tool lines to appear in the log (they never do), so the reload came
68 s into the turns, not at about 3 s. Cut-off point is reload + 120 s + 60 s,
so the turn needs to be still running at 180 s after the reload. It was not:

- 02:35:42 SIGUSR1; `Restart requested with 3 active work unit(s); deferring
  stop() ... (cap=120s)`. The 120 s cap from the config is live.
- 02:37:42 `Restart after-turn wait timed out after 120s with 2 still active;
  proceeding to stop()`, then `Stopping gateway for restart...`.
- 02:38:06 command 2 finished (`sleep 200` ended), 02:38:45.6 command 1 finished.
  `Shutdown phase: drain done ... drain took 56.68s, timed_out=False,
  active_at_start=2, active_now=0`: both ended inside the 60 s stop budget.
- Both rows `completed/success`, replies `retry test 2 done` and `retry test
  done`, one run each.

This confirms the other half of 8.1: a turn that ends inside the stop window
keeps its success, and the retry path is not taken. It is not the cut-off proof.

#### Second attempt: reload 2 s after the emit (cut and retried)

| # | command_id | thread_id | correlation |
| --- | --- | --- | --- |
| 1 | `aef119f0-6f46-457c-8255-9bdd5464faed` | `retry-proof2-20261021-1-a4a5b7a9` | `a4a5b7a9-6524-411e-b9ba-b808093e8ca6` |
| 2 | `ac9acb9b-e1df-46e3-b05c-65d80ab6cad5` | `retry-proof2-20261021-2-04ea50e3` | `04ea50e3-bec3-4764-941a-3afe68f7acbb` |

Timeline (UTC; Hermes local-time log lines converted):

| time | event |
| --- | --- |
| 02:46:08.64 / .71 | commands 1 and 2 emitted; journal rows created, `invocation.started` published |
| 02:46:10.76 | reload. `SIGUSR1 received ...`; `deferring stop() ... (cap=120s)`; `waiting on 2 active work unit(s)` (120 s remaining) |
| 02:46:11.6 / .7 | `Bloodbank lane=batch` / `lane=interactive stopped fetching` |
| 02:46:15 | both consumers `num_waiting 0`, `ack_pending 1` each |
| 02:46:40 | both turns in `current_tool: terminal` (the sleeps) |
| 02:48:10.85 | `Restart after-turn wait timed out after 120s with 2 still active` (120.1 s after the reload); `Stopping gateway for restart...` |
| 02:49:17.85 | `Shutdown phase: drain done at +66.95s (drain took 60.02s, timed_out=True, active_at_start=2, active_now=2)`; `interrupting remaining work` |
| 02:49:17.89 | adapter: `turn interrupted by Hermes restart ... after 189.1s: the stop drain ran out before it finished`, one per command |
| 02:49:18.2 / .5 | the `sleep` tool calls return `[Command interrupted]`, exit 130 (180.0 s and 179.0 s in) |
| 02:49:18.35 / .66 | adapter: `turn cut off by Hermes restart ... NAK, the next process re-dispatches the same thread` (command 2, then 1); `command not run by Hermes ... reason=gateway_restart ... NAK` |
| 02:49:22.9 | `bloodbank disconnected` |
| 02:49:36.75 | `Skipping .clean_shutdown marker`; `Gateway stopped (total teardown 85.86s)`; `gateway_state` `stopped`, `restart_requested true` |
| 02:49:56 | new process pid 3496787 |
| 02:51:03.4 | `Starting Hermes Gateway...`; `gateway_state` `starting` |
| 02:51:07.36 | `Bloodbank gateway connected` both lanes. The same second: both user prompts re-appear in their holocene-pm sessions (02:51:07) |
| 02:51:08.09 | both consumers `num_waiting 1`: both lanes pulling |
| 02:51:13 | both sessions issue the `sleep` tool call again |
| 02:54:34 / 02:55:14 | `sleep 200` and `sleep 240` return `exit_code 0` (201 s and 241 s later) |
| 02:54:36.44 | command 2 `completed/success`; reply `retry test 2 done` (17 chars) |
| 02:55:16.19 | command 1 `completed/success`; reply `retry test done` (15 chars) |
| 02:54:28.6 | `gateway_state running` (the other profiles have finished starting) |

**Total time from the reload to both Bloodbank lanes pulling: 297 s (4 min
57 s).** The pieces:

- 120 s: the after-turn wait (`restart_after_turn_timeout`).
- 86 s: stop and teardown, of which 67 s is the drain (60 s `restart_drain_timeout`
  plus 7 s to notify sessions) and the rest adapter and platform disconnects.
- 91 s: process exit, the 5 s `RestartSec`, and the new process getting to the
  Bloodbank adapter (the 1Password secret reads, `Starting Hermes Gateway` at
  02:51:03).

Add the length of the turn itself for the cut-off commands: command 2 finished
8 min 28 s after it was emitted, command 1 9 min 8 s. Both lanes ran about 3 min
20 s before `gateway_state` said `running` again, as in 8.2 and 8.3.

Proof points:

- **The drain gave up at the configured cap.** The wait ended 120.1 s after the
  reload, then the stop drain ran its full 60 s (`timed_out=True`) with both turns
  still alive. Total from reload to interrupt: 187 s.
- **Both turns were cut and NAKed, not closed.** The journal rows stayed
  `started` with no outcome through the whole gap (02:46 to 02:51). No
  `invocation.completed`, `failed` or `cancelled` fact existed for either
  correlation until 02:54:36 / 02:55:16, and neither `interrupted_by_shutdown` nor
  `hermes_*` failure code was written. The only adapter lines are the
  `interrupted ... NAK` ones above. The consumers kept `ack_pending 1` each
  (the NAKed message) with `num_waiting 0` until the new process fetched.
- **The new process re-dispatched the same thread.** holocene-pm has exactly one
  session per command, and each holds both attempts: the prompt, the first
  `terminal` call, `[Command interrupted]` / exit 130 at 02:49:18, the same prompt
  again at 02:51:07, a second `terminal` call, `exit_code 0`, and the single final
  reply. Sessions `20261020_224608_bb17e13e` (thread 1) and
  `20261020_224608_d545dc5e` (thread 2). The sleeps ran in full the second time.
- **Journaled once.** Each correlation has one `conversation.turn.started`, one
  `invocation.started` (both from the first process, 02:46:08), one
  `invocation.completed` and one `conversation.turn.completed`. The re-dispatch
  published no second `started`, because the row was already `started`.
- **No third run.** JetStream `delivered.consumer_seq` went 816 -> 817 -> 818
  (batch) and 75 -> 76 -> 77 (interactive): one first delivery and one redelivery
  each (`num_redelivered 1` from 02:51:07 until the ack), and neither moved again
  until an unrelated `pile-of-dumb-things-pm` batch command (seq 819, 02:54:22)
  arrived. No sleep ran a third time; both session transcripts end after the
  single second attempt.

#### Holocene wording through the cycle

Sampled every 2 s from `memo_common` (what `memo:trace` prints) and
`GET /api/modules/voice/memos` `.watcher.lanes`:

- **Drain** (02:46:12 to 02:49:37). Correct: `interactive: gateway restarting
  (draining), memo queued; it runs when the gateway is back (waiting on 2 in-flight
  turns), 1/2 in flight finishing; consumer ...: waiting=0 pending=0 ack_pending=1`
  and the lanes line `... gateway restarting (draining), 1/2 in flight, 0 queued |
  batch: gateway restarting (draining), 1/4 in flight, 0 queued`.
- **Between processes and the first seconds of the new one** (02:49:37 to 02:51:07,
  `stopped` then `starting`). Wrong, now fixed. The trace read `interactive:
  pulling (busy: 1/2 in flight, 0 queued ahead)` and the lanes line `pulling,
  1/2 in flight`, while `num_waiting` was 0 and nothing could pull for 90 s. The
  lane's `ok` is "a parked pull request or work in flight", and the NAKed message
  sits in `ack_pending`, so it counted as in flight and the `starting`/`restarting`
  exemption for pulling lanes treated that as a puller.
- **New process connected** (02:51:07 to 02:54:29, state `starting`): `pulling`,
  correct, since both lanes had parked pull requests from 02:51:08. After
  `running`: `interactive: pulling, 0/2 in flight, 0 queued | batch: pulling ...`.

Fix: Holocene `ed1d0bf` (`memo_common.gateway_lanes`). Only `num_waiting >= 1`
exempts a starting or restarting gateway; a non-draining lane with `ack_pending`
reads `held for re-run`, so the same states now print `gateway restarting, 1/2
held for re-run, 0 queued`. Covered by
`test_a_naked_turn_held_between_processes_is_restarting_not_pulling` (replays the
observed consumer numbers); `mise run memo:test` passes (160 + 78 tests) and
`holocene-inbox-watcher.service` was restarted on it. Not re-run live: it needs
another cut-off cycle to see the held state.

Facts this run settled that 8.3 had only from code: `restart_after_turn_timeout`
120 is loaded by the live gateway (`cap=120s` in the log), and
`shutdown_interrupt: retry` is read per lane (both lanes logged the NAK path).

Raw logs are in `/tmp/claude-retryproof/` (second attempt: `driver.sh`,
`driver.log`, `observe.log`, `holo.log`) and `/tmp/claude-retryproof1/` (first
attempt).
