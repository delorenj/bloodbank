# agent-state

Per-zellij-pane agent state, projected from the Bloodbank bus **and** from
observed reality, published to Redis for any surface to read.

```
bloodbank.evt.agent.>        ─┐
bloodbank.evt.conversation.> ─┼─► fold ──┐
deckard.evt.attention        ─┘          ├─► Redis ──► tab painter / Deckard / Nanoleaf
ps + zellij list-panes ──► reconcile ────┘
```

## Why it is not purely event-driven

Agent hooks publish over **core NATS** — `agent-hooks/core/nats_publish.py` says
it in its own docstring: *"open one TCP connection, PUB, drain with PING/PONG,
close."* At-most-once, no JetStream, no replay. And a failed publish is
deliberately **swallowed** (the hook returns 0 unless `BLOODBANK_HOOK_STRICT=1`,
which is set nowhere), because a hook must never fail the user's turn.

So events *will* be lost. For a state display that is fatal on its own: miss one
`session.ended` and a tab claims "working" forever, with nothing to correct it.
That failure has already happened here — tabs stuck showing a bell nothing could
clear.

Hence three legs:

| leg | rate | job |
|---|---|---|
| **events** | instant | paint the transition |
| **reconcile** | ~10s | ask what is *actually* true, correct drift |
| **TTL** | always | unrefreshed state expires, so staleness is **visible** |

**The rule: on conflict, observation wins.** An event-derived state must never
outlive its evidence. Any missed event self-heals within one reconcile period —
that is what buys robustness, not the transport.

Verified live: a `working` pane with no agent process decayed to `idle` in 21s
with `source=reconcile:no-agent-process`.

## Why Redis

Not for pub/sub — NATS is better at that. Redis holds the **level**. A consumer
that starts fresh (Deckard restarting, a painter launching) can ask *"what is
true right now?"* and get an answer immediately, instead of waiting for the next
event, which may be minutes away. A bus cannot answer that question.

## States

| state | meaning | ends when |
|---|---|---|
| `working` | an agent or subagent is doing something | turn ends, or reconcile finds no agent process |
| `attention` | it stopped and needs a human | a new turn, or TTL |
| `error` | the turn failed — 4xx, rate limit, out of tokens | a new turn, or TTL |
| `idle` | nothing running | — |

Precedence is `attention > error > working > idle`, matching Deckard's
`AgentState`: a human being asked a question outranks a turn that already failed
and is now just sitting there.

Two asymmetries that look like bugs until you know why:

- **Mid-turn traffic never clobbers a waiting state.** A tool call firing must
  not erase the bell that says "answer me". But a *new prompt* does reset it —
  the human has plainly moved on.
- **`attention` and `error` are not decayed by process absence.** They are
  addressed to a person and outlive the process that raised them, which is
  exactly when they matter most. They end by acknowledgement or TTL.

## Reading it

```bash
redis-cli --scan --pattern 'agentstate:pane:*'
redis-cli get agentstate:pane:Workspace:128
redis-cli subscribe agentstate:changes      # push, for consumers that want it
```

```json
{"session":"Workspace","pane":128,"state":"working","since":1788630159.76,
 "agent":"claude","cwd":"/home/delorenj/code/33GOD","source":"bloodbank.conversation.turn.started"}
```

A **missing key means unknown**, never idle. That distinction is the whole point
of the TTL: a dead projector must not be able to assert anything.

## Running

Host service, not a container — reconciliation needs the process table and the
zellij CLI.

```bash
systemctl --user enable --now agent-state
journalctl --user -u agent-state -f
```

Stdlib only. No virtualenv, no `pip install`.

| env | default |
|---|---|
| `BLOODBANK_NATS_HOST` / `_PORT` | `127.0.0.1` / `4222` |
| `AGENT_STATE_REDIS_HOST` / `_PORT` | `127.0.0.1` / `6379` |
| `AGENT_STATE_PREFIX` | `agentstate` |
| `AGENT_STATE_RECONCILE_SECS` | `10` |
| `AGENT_STATE_TTL_SECS` | `45` (forced to 3× reconcile if set lower) |
| `ZELLIJ_BIN` | `zellij` |

## Gotchas

- **`bloodbank.evt.agent.>` is not enough.** Prompt submission publishes as
  `bloodbank.conversation.turn.started` — the *conversation* domain. Subscribing
  only to `agent.>` silently misses every turn start. (Deckard currently has
  this bug.)
- **Only stamped events can be placed on a tab.** `zellij_pane_id` is added by
  `zellij_origin()` in the agent-hooks publisher. Unstamped events are skipped,
  not guessed at — `working_directory` alone cannot separate two tabs in the
  same repo.
- **Agent processes are matched by process NAME, never by command substring.**
  `codex mcp-server` and a hermes daemon's python both contain an agent name in
  their path without being an interactive agent; that exact class of false
  positive has corrupted state here before.
- **"I could not look" is not "it is gone."** If `ps` or the zellij CLI fails,
  reconcile decays *nothing*. A wedged zellij must not blank every tab.

## Tests

```bash
python3 -m pytest tests/ -q
```

The state machine is pure functions over dicts with an injected clock, so all of
the above is tested without NATS, Redis, or a terminal.
