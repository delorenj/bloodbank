# Hook hub

Every supported CLI invokes `bb-hook` once per native hook. The host daemon
resolves that native hook into the canonical lifecycle role and Bloodbank event,
selects behavioral handlers from `handlers.toml`, and owns event publication.
CLI configuration contains the adapter entry point; behavioral wiring belongs in
one registry.

```mermaid
flowchart LR
  CLI[Native CLI hook] --> Client[bb-hook]
  Client -->|Unix socket| Hub[Canonical role and event]
  Hub --> Sync[Bounded synchronous handlers]
  Sync -->|Context or native decision| CLI
  Hub --> Async[Supervised background handlers]
  Hub --> Publish[Single Bloodbank publisher]
  Publish --> NATS[NATS]
  NATS --> Candystore[Candystore]
  Hub --> Journal[Receipt journal and transactional outbox]
  Journal --> Facts[Background observation publisher]
  Facts -->|JetStream storage acknowledgement| NATS
  NATS --> Holocene[Holocene event collector and views]
```

Claude, Codex, Copilot, Hermes, Antigravity, Gemini, Kimi, and OpenCode have
adapters. Installed inventory separately reports whether each executable and
native configuration exist. OpenClaw is explicitly unsupported. Some Hermes
signals have no canonical agent lifecycle event; these can run local handlers.
Their receipt changes still have the registered `bloodbank.agent.hook.updated`
observability contract.

## Runtime ownership

The Unix socket keeps synchronous context and approval decisions on the local
CLI path. Background handlers and NATS publication are supervised by the hub.
Handlers receive bounded launch context for host integrations such as Orca and
Zellij, plus `BB_HOOK_HUB=off` to prevent recursion.

The internal `bloodbank-publish` handler is the sole publisher of the original
agent lifecycle fact for a native invocation. Observation revisions are separate
facts and do not invoke this handler. Cached legacy `publish.py` commands forward to the hub once its
ownership manifest is active. Explicit native invocation, event, or tool-call IDs
provide durable duplicate suppression. A fresh UUID is used when the CLI supplies
no usable identity; matching prompt text alone never suppresses legitimate work.
This is not an exactly-once guarantee for a transport retry without stable IDs.

`~/.config/33god/hook-hub/ownership.json` records the centralized concerns.
Legacy handlers and their installers honor that manifest. Pausing a central
handler does not silently restore its old native wiring.

## Concern applicability

Native adapter coverage and behavioral applicability are separate. A registered
native hook with no matching concern is not missing a hook. The registry's `clis`,
`on`, and `on_native` fields declare the supported intersection; wrappers record
explicit skips for absent project context or optional tools.

| Concern | Applicable CLIs and runtime requirements |
| --- | --- |
| Hindsight, skill reminder, skill lint | Portable where the native adapter provides the matching lifecycle role and required prompt, session, or file-edit payload. Session write-back needs a turn-end signal with the final message or a readable transcript: Claude, Codex, Kimi, Antigravity, Copilot, Gemini (see Hindsight session write-back). |
| CodeGraph prompt context | Claude, Codex, Copilot, Kimi, Gemini, OpenCode, Hermes; the working repository must have an index. |
| Code Review Graph status/update | Claude, Codex, OpenCode; the repository must have a Code Review Graph index. The before-commit decision is OpenCode-specific. |
| Project Notebook | Claude only: its canonical PJangler engine accepts Claude identities and SessionStart/SessionEnd. Other CLIs must not be relabeled as Claude. Non-repository work is skipped; the project must also be registered and configured in PJangler. |
| Merge forward | Matching session closure in a repository that owns the `33god-merge-forward` tuner worker. Other repositories are skipped. |
| Orca status | Claude, Codex, Copilot, Kimi, Hermes, Antigravity; an Orca pane and usable endpoint are required. |
| Nanoleaf | Claude lifecycle and attention signals, with the existing panel runtime. |
| Sound notifications | Claude/Codex attention signals, Codex turn completion, and OpenCode session closure. |
| Zellij attention and Git checkpoint | OpenCode only; attention requires a Zellij pane, and the existing checkpoint project opt-out is preserved. |

Project Notebook, Nanoleaf, CodeGraph, Code Review Graph, Orca, CommonProject/PJangler projections,
and guarded project fallbacks consult the same ownership contract so ordinary
reinstallation does not restore retired native owners. Standalone installations
without a central owner retain their existing behavior.

The managed `~/.local/bin/code-review-graph` launcher delegates to the installed
uv tool. While the hub owns its concern, `install` and its `init` alias receive
`--no-hooks`; other commands and arguments pass through. The tool package remains
upstream-owned. `hub:install` and combined cutover installation restore this link
if a uv tool reinstall replaces it. To refresh just the launcher:

```sh
python3 services/hook-hub/tool_guards.py --install
```

## Installation and cutover

```sh
mise run hub:install
python3 services/hook-hub/cutover.py --project /path/to/project
python3 services/hook-hub/cutover.py --project /path/to/project --apply --install
python3 services/agent-hooks/sync.py --check-installed --json
python3 services/agent-hooks/health/hook_healthcheck.py --json
```

The first cutover command is a read-only plan. The combined apply/install captures
Codex native trust before pruning known legacy handlers, renders and installs the
canonical adapters, then preserves existing trust choices while trusting the new
managed entry points. It discovers Hermes profiles and alternate Codex runtime
homes. It preserves foreign hooks and configuration properties.

Verify the native CLI loader as well as static inventory before activating staged
behavioral rows:

```sh
python3 services/hook-hub/cutover.py --project /path/to/project --activate
```

Activation refuses to proceed while inspected legacy managed commands remain.
The registry reloads on mtime changes. Daemon (`hub.py`) code changes require a
service restart; handler code (`concerns.py`, `hindsight.py`) runs as a fresh
process per hook and does not.
Native CLIs that cache their hook configuration need a fresh session for newly
added native hook types. The legacy publisher compatibility path covers existing
registered publisher commands.

## Receipts and Holocene

The metadata-only SQLite journal records received, selected, started, succeeded,
failed, timed-out, skipped, interrupted, and duplicate-suppressed work. Background
children remain supervised until completion. Session-end retention waits for
pending candidate writes from the same session.

The read-only HTTP API binds to loopback by default:

| Endpoint | Result |
| --- | --- |
| `/v1/hooks/status` | Registry, mappings, installed wiring, and aggregate activity |
| `/v1/hooks/invocations` | Paginated execution receipts |
| `/v1/hooks/invocations/{id}` | One receipt with its lifecycle timeline |

History accepts `cli`, `native`, `role`, `handler`, `status`, `limit`, and `offset`.
This API is a local operator diagnostic interface. Holocene consumes Bloodbank
events and owns its collected read model; it does not proxy this API or read the
hub's SQLite database. Configuration proves wiring; a receipt proves observed
execution. Quiet hooks remain visibly unobserved.

Two schema-backed CloudEvents feed that collector:

| Type | Data |
| --- | --- |
| `bloodbank.agent.hook.updated` | The latest complete invocation revision with execution outcomes and recent timeline, including unmapped native signals, skips, failures, recovery, and deduplicated requests; bursts are coalesced (below) |
| `bloodbank.system.hook.updated` | Either a full `snapshot` of installed wiring and hub health, or a compact `heartbeat` containing health and aggregate activity |

Both carry `schema_version`, a persistent UUID `hub_id`, and equal monotonic
`sequence`/`revision` values. UUIDv5 event IDs remain unchanged on transport
retries; the same ID is also the `Nats-Msg-Id` header. Consumers deduplicate
envelope IDs and only apply a newer revision to the same hub/invocation. The
producer's observed timestamp is retained; broker delivery time cannot make old
health appear fresh. System observations expire after 90 seconds.

Full snapshots are sent at startup, when inventory/configuration changes, and
hourly so a new collector can bootstrap within broker retention. Compact health
is sent every 30 seconds and only merges into a snapshot from the same `hub_id`.
The September 13 deployed inventory yields a 221,261-byte full event and a
23,550-byte compact event, below the broker's 1 MiB limit. All events are bounded
to 900,000 bytes including envelope metadata. Receipt projections retain the
latest 512 timeline entries with explicit `timeline_total` and
`timeline_truncated` fields. Snapshot fields are selected through the shared schema allowlist, so newly added
raw config, command, or environment keys cannot silently enter the feed.

### Coalescing (2026-09-23)

One native hook mutates its receipt about 13 times in a second or two: the
claim, then every handler's select, start and finish. Publishing each of those
as a full revision put ~20 `bloodbank.agent.hook.updated` events per second on
`bloodbank.evt.>` under a busy multi-agent session (about 93% of everything
Candystore stored), although Holocene keeps only the newest revision per
invocation. The outbox now coalesces instead:

- An unpublished revision is **superseded**, not queued behind: the next change
  to the same invocation deletes it and enqueues the new projection under a new,
  higher `sequence`/`revision` in the same transaction. A row already on the
  wire is simply superseded; its late acknowledgement deletes nothing.
- A **settled** invocation (no execution selected or started, status no longer
  `received`) is due at once. An unsettled one waits for
  `HOOK_HUB_OBSERVATION_DEBOUNCE` seconds of quiet, and never longer than
  `HOOK_HUB_OBSERVATION_MAX_DELAY` after its first unpublished change, so a
  long handler still shows as `started` and a duplicate storm cannot starve it.
- Snapshots and heartbeats coalesce by kind the same way, so a broker outage
  releases only the latest of each instead of a backlog.

The payload contract is unchanged: every published event is still one full,
immutable, schema-valid revision with a unique UUIDv5 ID. What changed is that
intermediate revisions nobody reads are no longer published, so `sequence` has
gaps. The timeline inside each revision still records every transition.

Receipt mutations and the serialized observation are committed in one SQLite
transaction. The background worker retries from its outbox until a matching
JetStream PubAck confirms storage in `BLOODBANK_EVENTS`; PONG alone never removes
the row. A crash between that acknowledgement and outbox removal may redeliver
the same event ID. Broker deduplication is time-bounded, so consumers must retain
their own ID/revision checks. Publication is independent of handler execution;
outage recovery never reruns a behavioral handler. Pending observations survive
local receipt pruning.

Startup automatically backfills previous receipts as latest full projections in
resumable batches of 50. Each invocation gets a durable migration marker; a live
mutation also creates that marker, preventing an older backfill from replacing
newer state. Backfill does not replay old handlers or original lifecycle events.
The status metadata `hub.observation_delivery` reports pending count, last
acknowledged sequence/time, and a sanitized error class. An unavailable broker
can grow the durable outbox; inspect that counter and runtime disk capacity when
an outage persists.

Deployment of these producer changes requires only:

```sh
systemctl --user restart hook-hub.service
```

The existing socket unit keeps its pathname. The receipt database upgrades
additively in place; no new dependency, broker topology, or runtime path is
required. The outbox uses the existing `BLOODBANK_NATS_HOST` /
`BLOODBANK_NATS_PORT` configuration and preserves queued observations when
publication is temporarily disabled.

A publication receipt marked `sent` means NATS transport succeeded. It is not a
Candystore persistence acknowledgment. Durable delivery acceptance must separately
look up the event ID in Candystore. Receipts do not store prompts, transcripts,
stdout, stderr, or environment values.

## Hindsight recall and briefing

`hindsight-recall` runs on every prompt, so it has to be fast and send the right
query. Since the 2026-09-26 remediation (DeLoContainers
`stacks/ai/hindsight/docs/2026-09-26-leverage-audit.md`, plan item 1a/1b) it
works like this:

1. **Query hygiene.** Harness wrappers are dropped whole: `<system-reminder>`,
   `<task-notification>`, `<teammate-message>`, `<local-command-*>`,
   `<command-name>`/`<command-message>`, Codex's `<send_user_message_question_reply>`,
   `<environment_context>`, `<turn_aborted>` and the like, plus any other kebab- or
   snake-case `<tag>…</tag>` that opens a line. `<command-args>`, `<bash-input>`
   and `<pasted_content>` are the user's own words, so their tags go and their
   text stays. A leading slash-command token goes too (`/review-pr 123 …` →
   `123 …`); a leading path does not. If fewer than 24 characters remain, recall
   is skipped and a `recall_skipped` journal event records why.
2. **Length cap.** The server rejects queries over 500 cl100k tokens. Do not raise
   that limit: the reranker caps input at 512 tokens, so a long query only buys
   the most expensive rerank. The hub keeps the query under 400 tokens with
   tiktoken when the hub's interpreter can import it. Otherwise it uses a
   1,000-char cap, which is the live path because `/usr/bin/python3` has no
   tiktoken. It keeps the head (60%) and the tail (40%), since the ask is usually
   at one end. A `400 Query too long` is retried once at half the length, or
   shorter when the server's reported count says half is still over (emoji,
   braille and block-drawing pastes run ~3 tokens a char).
   Measured on 2026-09-26 against the server's own tokenizer: 190 real prompts
   that hit the 1,000-char cap came out at 191-350 cl100k tokens (median 247),
   and none reached 400.
3. **Banks.** The synchronous path reads **only the primary bank**, plus the
   agent's personal bank when the registry declares a `write_bank`. The primary
   bank gets `mid`/2048 and anything extra gets `low`/1024. Extra banks are
   opt-in:

   | Opt-in | Adds |
   | --- | --- |
   | `<main checkout>/.hindsight/recall-banks` | One bank per line, `#` comments allowed. It sits next to the `.hindsight/bank` override and is read from the primary checkout, so worktrees share it. Use it to give an infra-flavored repo `infra`. |
   | agent registry `hindsight.recall_banks` | The banks a Hermes agent row declares |
   | `HINDSIGHT_RECALL_GENERAL=1` | `general` (always on before 2026-09-26) |
   | `HINDSIGHT_GLOBAL_BANKS="a b"` | Space-separated banks. The default is now empty (it was `infra`). |
   | `HINDSIGHT_ANCESTRY=1` | Superproject banks of a submodule (on by default before 2026-09-26) |
   | `HINDSIGHT_FANOUT=1` | Dream-graph neighbours (on by default before 2026-09-26) |

   `HINDSIGHT_RECALL_MAX_BANKS` (default 8) caps the list and never drops the
   personal bank.
4. **`--prefer-observations`.** The hub drops raw facts that an observation it
   returned already consolidates. On a bank with no observations it still
   returns facts (checked on `plane`). Set
   `HINDSIGHT_RECALL_PREFER_OBSERVATIONS=0` to turn it off.
5. **A real deadline.** Each bank runs as its own CLI process. When
   `HINDSIGHT_RECALL_TIMEOUT` (default 8s, measured from handler start) runs out,
   the hub kills every process still going and returns what finished. That
   leaves 3s of margin under the registry's 11s `timeout_ms`.
6. **Journal.** Each `recall` event in
   `~/.agents/journal/sessions/<cli>-<session>.jsonl` carries `query_len_raw`,
   `query_len_clean`, `query_len_sent`, `deadline_s`, `elapsed_ms` and a
   `per_bank` list. Each entry has `bank`, `status` (`ok`, `empty`, `timeout`,
   `http_400`, `not_found`, `error`), `latency_ms` and `results`, plus
   `retried` and `detail` when they apply.

`hindsight-briefing` runs once at session start (not on `resume`). It GETs the
primary bank's `briefing`, `pitfalls` and `rules` mental models in parallel from
`/v1/default/banks/{bank}/mental-models/{id}`. The bank templates seed those ids.
The URL and key come from `HINDSIGHT_API_URL`/`HINDSIGHT_API_KEY`, falling back to
`~/.hindsight/config`, as the CLI does. A model that is missing (404), fails, or
still says `Generating content...` is skipped silently. Whatever is ready is
injected under a `# Hindsight briefing` header, capped at
`HINDSIGHT_BRIEFING_MAX_CHARS` (6,000). The whole fetch has a 0.5s budget
(`HINDSIGHT_BRIEFING_TIMEOUT`), and `HINDSIGHT_BRIEFING=0` turns it off.

Every `HINDSIGHT_*` knob above can be set in the agent's shell, because
`bb-hook` forwards each one by exact name, or in the hub's service environment.
The one exception is `HINDSIGHT_RECALL_QUERY_MAX_TOKENS`. Its name contains
`TOKEN`, so `bb-hook`'s secret-shaped gate drops it on purpose. Set that one in
the service environment.

Handlers are spawned fresh for every hook, so edits to `hindsight.py` and
`concerns.py` apply on the next prompt with no restart. Registry edits apply on
mtime. Only `hub.py` changes need `systemctl --user restart hook-hub.service`.

The legacy shell hook `~/.agents/hooks/hindsight/hindsight-recall.sh` is not
maintained and did not get these changes. It exits as soon as the ownership
manifest names the hub as owner, which it does for all eight CLIs, and no native
CLI config calls it. `~/.claude/hooks` is a symlink to `~/.agents/hooks`, so
there is no second copy. Its last journal write was 2026-09-13.

## Hindsight session write-back

Since 2026-09-26 (audit plan item 2a), every substantive turn leaves its
**decisions and outcomes** in the repo's bank. The code is in
`session_capture.py`.

**What it replaced.** SessionEnd used to retain `Session completed. Files
edited:` plus the first 400 characters of every edit, with context
`session-summary`. That text was code, not outcomes. The `Session outcome` line
never appeared, because Claude's SessionEnd payload carries only `reason`. 191
of 278 session ends retained nothing, because nothing had been edited. Codex
never wrote at all: its edits arrive as `exec` code that calls
`tools.apply_patch("*** Begin Patch\n...")`, and the old path looked for a
`patch` key. Failed retains were dropped, and every write went through the CLI
with a 45s timeout. That path is gone. `hindsight-retain` now records file
paths only.

### Per turn (buffering, no network)

| Hook role | Handler | Buffers |
| --- | --- | --- |
| `prompt_submit` | `hindsight-turn` | The user's ask, cleaned by `hindsight.clean_query` (the recall path's harness-XML and slash-command hygiene) and clipped to 1,200 chars (head and tail). A harness-only prompt, such as a `<task-notification>`, buffers nothing. |
| `post_tool` | `hindsight-retain` | Paths of edited files, relative to the repo. They come from the input of edit tools (`Edit`, `Write`, `MultiEdit`, Kimi `Edit`/`Write`, Copilot `edit`/`create`, …), from any `*** Add/Update/Delete File:` / `*** Move to:` header anywhere in the tool input (Codex sends the patch under `command`; it can also sit inside a JS string literal), and from shell heredoc writes (`cat > f <<EOF`, `cat <<EOF >> f`, `tee [-a] f <<EOF`), excluding `/tmp` and `/dev`. File content is never recorded. |
| turn end (`turn_completed`, or native `Stop` for Antigravity) | `hindsight-turn` | The final assistant message closes the turn as ask + outcome + files. Long fenced code blocks become `[code block elided: N lines]`, and the outcome is clipped to 2,400 chars. A turn is dropped if its outcome is under 80 chars and it edited nothing. Interrupts are dropped, and so is a repeated Stop for the same turn. |

The buffer is a JSON state file per session, kept in
`$XDG_STATE_HOME/33god/hook-hub/capture/<cli>-<session>.json` (mode 0600,
written atomically under a sidecar `flock`). A session's first event fixes its
bank, repo and document id. For Antigravity the bank comes from the payload's
workspace, not from the hook's cwd.

### CLI coverage

| CLI | Turn end | Final message from | Ask from | Live-verified |
| --- | --- | --- | --- | --- |
| Claude | `Stop` | `last_assistant_message` (hook schema 2.1.283); transcript tail as fallback | `UserPromptSubmit` | yes |
| Codex | `Stop` | `last_assistant_message` (0.157 schema); rollout tail as fallback | `UserPromptSubmit` | yes (edit paths from `apply_patch` and heredocs) |
| Kimi | `Stop` | the session's `~/.kimi-code/sessions/*/session_<id>/agents/main/wire.jsonl` (its Stop has no message or path) | `UserPromptSubmit` | fixture only |
| Antigravity | `Stop` (role `session_end`, needs `fullyIdle`) | `transcriptPath` (`PLANNER_RESPONSE`) | the transcript's `<USER_REQUEST>` (it has no prompt hook) | fixture only |
| Copilot | `agentStop` | `transcriptPath` (`events.jsonl`) | `userPromptSubmitted` | fixture only |
| Gemini CLI | `AfterAgent` | `prompt_response` | `BeforeAgent` | no (the `gemini` alias runs `agy`) |
| Hermes | not covered | `on_session_end` carries only `session_id`/`completed`/`interrupted`; its Hindsight plugin already retains every turn to the agent bank | | |
| OpenCode | not covered | `session.idle` carries neither a message nor a transcript path; the plugin would have to forward the last assistant text part | | |

Subagent stops are not captured. A subagent's tool calls carry the parent's
session id, so their edits land in the parent's next turn, and the parent's
final message is the outcome.

### Flush (HTTP, append-mode, one document per session)

A flush sends the buffered turns to
`POST /v1/default/banks/{bank}/memories` with `async: true` and one item:

```json
{"content": "[{\"role\":\"system\",\"content\":\"Session in DeLoContainers (claude) on big-chungus, started …\"},
              {\"role\":\"user\",\"content\":\"<ask>\"},
              {\"role\":\"assistant\",\"content\":\"<outcome>\\n\\nFiles edited: a.py, b/c.md\"}, …]",
 "document_id": "session-<cli>-<session_id>", "update_mode": "append",
 "observation_scopes": "shared", "strategy": "conversation" (only if the bank defines it),
 "context": "Agent session in <repo> (<cli>): each user message is a request, …",
 "tags": ["agent:<cli>", "host:<host>"],
 "metadata": {"source": "hook-hub/session-capture", "cli": "…", "session_id": "…", "repo": "…", "host": "…"},
 "timestamp": "<first turn of the batch>"}
```

It goes over HTTP rather than through the CLI because the CLI cannot set
`update_mode`, `observation_scopes` or `strategy`. It flushes:

- when the buffer passes `HINDSIGHT_CAPTURE_FLUSH_CHARS` (9,000, about three
  3,000-char extraction chunks), so a long session writes as it goes;
- at session end (`hindsight-session-end`, forced; it also marks the session
  closed);
- from the sweeper, when a session has been idle for `HINDSIGHT_CAPTURE_IDLE_S`
  (2h). This covers a session that died without a SessionEnd, and the
  long-lived zellij panes that rarely send one. If the session resumes later,
  its next flush appends to the same document.

The system header goes only into a session's first batch. The `context` field
carries the repo and CLI on every chunk.

**Why a JSON conversation array, not text.** This was measured on 0.10.1 on
2026-09-26 with a scratch bank and three appends to one document. With append
mode, the server prepends the stored text as a separate item and diffs chunks
by index and hash:

| Appended as | 2nd append | 3rd append |
| --- | --- | --- |
| plain text | 1 unchanged, 1 new | `0 unchanged, 2 changed`, then `Delta retain: no unchanged chunks … falling back to full retain`, which re-ingested everything and invalidated 7 observations |
| JSON array | 1 unchanged, 1 changed, 1 new | 2 unchanged, 1 changed, 1 new |

The stored text is the joined string, so the plain-text form re-chunks
differently on every later append. JSON arrays are merged into one array and
chunked at turn boundaries, which is prefix-stable. Each flush therefore
re-extracts only the stored document's last chunk plus the new turns. The
0.9.1 source has the same append path (`merge_json_array_parts`, per-item
`_chunk_contents_for_delta`), and 0.9.1 also accepts `update_mode`,
`observation_scopes: "shared"` and `operation_id`.

**Why `observation_scopes: "shared"`.** A consolidation scope is a fact's full
tag set. `agent:claude` and `agent:codex` facts about the same repo would
otherwise build separate observations. `shared` resolves to one untagged scope
per bank, and each repo has its own bank, so this is one scope per repo. In
0.10.x consolidation batches are keyed by that resolved scope. In 0.9.1 they
were still keyed by raw tags (#3954). The provenance tags stay on the facts,
because they no longer fork anything. In the probe, observations came back with
`tags: []` and the facts kept `agent:claude, host:…`.

`strategy: "conversation"` matches the per-content-type strategy names of the
`hindsight-coding-agents` reference design. It is sent only when the bank's
`GET …/config` lists that strategy in `retain_strategies`, one small GET per
flush. Naming a strategy the bank lacks is harmless, but the server logs a
WARNING for every such retain, and almost no bank defines one yet. The first
flushes logged six of them before this check existed. Once a bank template
adds a `conversation` strategy, sessions start using it with no change here.
`HINDSIGHT_SESSION_STRATEGY=` (empty) never sends one.

### Delivery, retry and the sweeper

Each batch goes through these states: `sending`, then `submitted`, then
confirmed (`completed`), after which it is forgotten.

- **Idempotent.** `operation_id` is a UUIDv5 of (bank, document, turn range,
  attempt). If an acknowledgement is lost (a timeout, so the outcome is
  `unknown`), the next pass asks the server for that operation before sending
  anything. `not_found` means the batch never arrived, and it is re-sent under
  the same id. Anything else is adopted as the batch's status. No network call
  runs under the session lock.
- **Confirmed, not assumed.** With `async: true`, an HTTP 200 only means the
  batch was queued. Extraction can still fail later, for example on the LLM
  key's daily cap, and a failed extraction stores nothing. After 30s a
  `submitted` batch is checked through `GET …/operations/{id}`. `completed`
  forgets it. `failed` or `cancelled` re-sends it under a new attempt id.
- **Backoff and cap.** A batch that definitely did not land (HTTP error,
  operation failed) waits 1m, 5m, 15m, 1h, 3h, 6h, 12h and 12h between tries,
  about 34h in total, which outlasts a spent daily cap. New turns join a batch
  that is still waiting, so order is kept and a failing server gets one
  request. After `HINDSIGHT_CAPTURE_MAX_ATTEMPTS` (8), or 3 days, the exact
  request is written to `capture/dead-letter/<doc>-<first>-<last>.json`
  instead of being dropped. Replay one with
  `curl -X POST "$url" -H 'Content-Type: application/json' -d "$(jq .request <file>)"`.
- **Sweeper.** Every captured turn and session end sweeps the other sessions'
  buffers (up to 8, within 3s). The `hindsight-capture-sweep.timer` user unit
  runs `session_capture.py sweep` every 10 minutes for the hours when no agent
  is running. A closed session with nothing left is deleted. So is one idle for
  a day with nothing pending. `python3 session_capture.py status` lists live
  buffers.

Install the timer with the other user units:

```sh
ln -sf ~/code/33GOD/bloodbank/services/hook-hub/systemd/hindsight-capture-sweep.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now hindsight-capture-sweep.timer
```

### Verification (2026-09-26, server 0.10.1)

- **Synthetic session, scratch bank.** Five turns, driven through the exact
  handler commands the hub spawns, with a 1,500-char flush threshold. Turns
  1-3 were flushed by size, and turns 4 and 5 by two session ends. The stored
  document held 11 messages (one system header and five user/assistant pairs),
  with no code body and no harness XML. Extraction produced one world fact
  per decision: the queue root cause and fix, retries with jitter, the burst
  canary, the rollback plan, and alert ownership. It also produced five
  untagged (shared-scope) observations. All three operations were confirmed
  by the sweeper, and the state file was deleted.
  This document stayed under one 3,000-char chunk, so each append replaced
  that chunk and the server fell back to a full re-ingest. That is expected:
  prefix stability starts once a batch fills its chunk, which the 9,000-char
  production threshold guarantees for mid-session flushes. A resumed tiny
  session costs one small re-extraction.
- **Real Codex (`codex exec` 0.157, live hooks and hub).** In the first
  session, Codex wrote a file through a shell heredoc. That exposed the need
  for heredoc detection (added). The second session used `apply_patch`. Its
  PostToolUse payload is `tool_name: "apply_patch"` with the patch under
  `command`, and the path was captured. Both sessions were flushed at
  SessionEnd (`codex exec` sends one) and confirmed. The two sessions' facts
  consolidated into one observation.
- **Real Claude (`claude -p` 2.1.283 in this repo, live hooks and hub).** The
  briefing, the ask, the `last_assistant_message` outcome (clipped to 2,391
  chars) and the SessionEnd flush all went into bank `bloodbank` as
  `session-claude-a2a342c1-…`, and the operation completed. It produced 10
  world/experience facts, each one a design decision or coverage statement.
  The document carries `observation_scopes: shared` and tags
  `agent:claude, host:big-chungus`.
- The scratch banks (`zz-capture-probe-*`, `zz-capture-e2e-*`,
  `zz-capture-codex-e2e`) were deleted, bringing the bank count back to 202.

### Journal and receipts

The session journal gets these metadata events: `turn_captured` (turn, source,
sizes, file count, buffered chars), `turn_skipped` (reason),
`session_flush` (bank, document, operation, turn range, attempt, trigger,
status, and the error when there was one), `session_flush_confirmed`,
`session_flush_failed`, `session_flush_unverifiable` and
`session_flush_abandoned` (with the dead-letter path). Receipt reasons include
`ask_buffered`, `edit_paths_recorded`, `turn_buffered`,
`turn_buffered_flush_submitted`, `turn_trivial`, `session_flush_submitted`,
`session_flush_deferred_for_retry` (a failed receipt, visible in Holocene) and
`nothing_to_flush`.

### Knobs

| Variable | Default | Purpose |
| --- | --- | --- |
| `HINDSIGHT_CAPTURE` | `1` | `0` turns capture off |
| `HINDSIGHT_CAPTURE_FLUSH_CHARS` | `9000` | Buffered size that triggers a mid-session flush |
| `HINDSIGHT_CAPTURE_IDLE_S` | `7200` | Idle time after which the sweeper flushes a session |
| `HINDSIGHT_CAPTURE_ASK_CHARS` / `_OUTCOME_CHARS` | `1200` / `2400` | Per-turn caps (each JSON turn stays under the 3,000-char chunk) |
| `HINDSIGHT_CAPTURE_MIN_OUTCOME` | `80` | Shorter outcomes with no edits are trivial |
| `HINDSIGHT_CAPTURE_MAX_ATTEMPTS` | `8` | Delivery attempts before dead-lettering |
| `HINDSIGHT_CAPTURE_MAX_AGE_S` | `259200` | Oldest a batch may get before dead-lettering |
| `HINDSIGHT_CAPTURE_DIR` | `$XDG_STATE_HOME/33god/hook-hub/capture` | Buffers and dead letters |
| `HINDSIGHT_SESSION_STRATEGY` | `conversation` | Named retain strategy, sent when the bank defines it; empty never sends one |

`HINDSIGHT_CAPTURE`, `_FLUSH_CHARS`, `_MIN_OUTCOME` and
`HINDSIGHT_SESSION_STRATEGY` can be set in the agent's shell, because
`bb-hook` forwards those exact names. `HINDSIGHT_CAPTURE=0` keeps a scripted
or throwaway session (`claude -p` jobs, probes) out of the repo's memory. The
rest are read by the sweeper too, which runs without a caller, so set them in
the hub's service environment and the sweep unit. As with recall, a repo can
opt out with `"hindsight-turn"` in the `hooks.disabled` list of
`.agents/local.json`.

## Deadlines and failure behavior

Ordinary client calls have a 3-second total deadline and a 2.5-second synchronous
budget. Prompt hooks that recall Hindsight use a 15-second client deadline within
a 16-second native timeout, with up to 14 seconds of shared synchronous work.
The registry kills the recall handler at 11 seconds, but it stops itself at
`HINDSIGHT_RECALL_TIMEOUT` (8s) and kills its own CLI children first. Each other
handler has its own registry timeout. Sync handlers run one after another, so a
Claude prompt's worst case is skill-reminder (1s), then recall (about 8.5s with
interpreter start), then codegraph-prompt (2s), then hub-selftest (0.5s). That
totals about 12s inside the 14.5s budget.

The client fails open on unavailable sockets, malformed replies, or elapsed
deadlines. A deliberate, valid native denial is preserved. Hung handler process
groups are terminated and recorded as timed out. A malformed registry retains the
last good configuration and surfaces the error in Holocene. None of these states
are reported as successful execution.

On service shutdown, the hub stops accepting new connections and drains accepted
requests and background work for up to two seconds. Publication has two reserved
slots, so long behavioral jobs cannot starve it. Remaining work is recorded as
`interrupted` with reason `shutdown_grace_expired`; an unfinished publisher has
outcome `unknown`. It is not automatically replayed. The service uses
`KillMode=mixed` and `TimeoutStopSec=5s` so systemd allows that drain before
terminating the remaining process group. Status reports `draining` while it runs.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `BB_HOOK_HUB` | unset | `off` disables client recursion |
| `BB_HOOK_SOCKET` | `$XDG_RUNTIME_DIR/33god/hook-hub.sock` | Unix socket |
| `BB_HOOK_DEADLINE` | `3.0` | Client total deadline, seconds |
| `HOOK_HUB_REGISTRY` | `handlers.toml` beside the daemon | Behavioral registry |
| `HOOK_HUB_SYNC_BUDGET` | `2.5` | Default shared synchronous deadline |
| `HOOK_HUB_MAX_SYNC_BUDGET` | `14.0` | Maximum shared synchronous deadline |
| `HOOK_HUB_ASYNC_SLOTS` | `8` | Concurrent background handlers |
| `HOOK_HUB_PUBLISH_SLOTS` | `2` | Reserved publisher concurrency |
| `HOOK_HUB_SHUTDOWN_GRACE` | `2.0` | Shutdown drain, capped at two seconds |
| `HOOK_HUB_RECEIPTS` | `$XDG_STATE_HOME/33god/hook-hub/receipts.sqlite3` | Receipt database |
| `HOOK_HUB_LOG` | `$XDG_STATE_HOME/33god/hook-hub/hub.log` | Rotating diagnostic log |
| `HOOK_HUB_HTTP_HOST` | `127.0.0.1` | Read-only API bind address |
| `HOOK_HUB_HTTP_PORT` | `8685` | Read-only API port; zero disables it |
| `HOOK_HUB_PUBLISH` | `true` | Central publisher enabled |
| `HOOK_HUB_OBSERVATIONS_PUBLISH` | value of `HOOK_HUB_PUBLISH` | Publish durable observation facts; a separate flag permits isolated producer tests |
| `HOOK_HUB_OBSERVATION_INTERVAL` | `30` | Compact health observation interval, seconds |
| `HOOK_HUB_OBSERVATION_DEBOUNCE` | `2.0` | Quiet window before an unsettled invocation's latest revision publishes, seconds; `0` publishes every coalesced revision at once |
| `HOOK_HUB_OBSERVATION_MAX_DELAY` | `10.0` | Longest an unsettled invocation's change can wait for quiet, seconds |
| `BLOODBANK_ENABLED` | `true` | Global publication switch; `false` also pauses observation delivery |

Unset XDG state/runtime paths use the user's standard local state and runtime
locations. Runtime files remain outside the source checkout.

## Development

```sh
python3 -m pytest services/hook-hub/tests services/agent-hooks/tests
```

Add a behavioral row to `handlers.toml`, bind it with `on` lifecycle roles or
`on_native`, and optionally narrow it with `clis`. Sync handlers return context or
native decisions. Async handlers emit supervised outcomes. Retire any equivalent
native registration before enabling the new row.
