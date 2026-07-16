# Bloodbank Event Naming Contract — v1

**Status:** Locked  
**Adopted:** 2026-05-14  
**Supersedes:** the `<domain>.<entity>.<action>` shape documented in
`compose/nats/streams.json` and the open-ended `type` regex in the legacy
Holyfields `_common/cloudevent_base.v1.json`.

This document is the single source of truth for Bloodbank event identity.
Any conflict between this doc and other artifacts (CLAUDE.md, streams.json,
schemas, code) is a defect in the other artifact.

**Schema home:** the canonical schema tree lives in this repo at
`bloodbank/schemas/` (see §12). Holyfields previously hosted the tree; that
copy is retained only as a transitional fallback for downstream consumers
and will be removed once they cut over. Future re-extraction into a neutral
registry repo is appropriate once there are multiple serious consumers
outside Bloodbank.

---

## 1. The non-negotiable rule

> Bloodbank event types describe **provider-neutral facts**. Provider, model,
> CLI, agent, session, thread, and IDs belong in `source`, `subject`, `actor`,
> envelope metadata, or `data`. They do **not** belong in `type`.

If a Claude session and a Copilot session produce the same semantic fact,
they emit the **same `type`** with different `actor` metadata. That is the
whole point.

---

## 2. CloudEvents `type` — shape and regex

Every event's CloudEvents `type` is exactly five dotted tokens:

```
bloodbank.v1.<domain>.<entity>.<action>
```

The vendor prefix (`bloodbank`) and contract version (`v1`) are baked into
the type so a consumer reading a single envelope knows the contract and its
version without parsing `dataschema`.

Regex (anchored, lowercase, underscores allowed in body tokens):

```
^bloodbank\.v[0-9]+\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$
```

Valid:

```
bloodbank.v1.conversation.message.appended
bloodbank.v1.llm.response.received
bloodbank.v1.cli.stdout.appended
bloodbank.v1.agent.invocation.started
```

Invalid (contract violation):

```
agent.session.started                  # legacy 3-token shape; no vendor/version
bloodbank.v1.agent.claude.response     # provider name in type
bloodbank.v1.copilot.message.generated # provider name in type
bloodbank.v1.agent.session.response    # response is not an action verb
bloodbank.v1.conversation.message      # only 4 tokens
bloodbank.V1.x.y.z                     # uppercase forbidden
```

Token rules:

- `bloodbank` and `v<N>` are literal segments 1–2.
- `domain` (segment 3) MUST be in §6 allowlist.
- `entity` (segment 4) MUST be in §7 allowlist.
- `action` (segment 5) MUST be in §8 allowlist and MUST match the tense
  required by §5 for the envelope `kind`.
- Every body token is `[a-z][a-z0-9_]*` (lowercase, may contain underscores,
  must start with a letter). Hyphens are not allowed.

---

## 3. NATS subject — shape, kind marker, stream mapping

NATS subject mirrors `type` but inserts a **kind marker** as the second
segment so JetStream streams can be bound by transport-level routing
without parsing the envelope body:

```
bloodbank.<kind>.v1.<domain>.<entity>.<action>
```

`<kind>` ∈ `{ evt, cmd, rpy }`:

| Marker | Envelope `kind` | Stream               | Subject filter       |
| ------ | --------------- | -------------------- | -------------------- |
| `evt`  | `event`         | `BLOODBANK_EVENTS`   | `bloodbank.evt.v1.>` |
| `cmd`  | `command`       | `BLOODBANK_COMMANDS` | `bloodbank.cmd.v1.>` |
| `rpy`  | `reply`         | `BLOODBANK_COMMANDS` | `bloodbank.rpy.v1.>` |

Subject is 6 tokens. `type` stays 5 tokens. The subject's kind marker is a
**transport-only** redundancy with the envelope `kind` field — consumers
MUST treat `envelope.kind` as authoritative; the subject marker exists only
so NATS can route without deserializing.

The pair `(domain, entity, action)` is identical across subject and type.

Example for `conversation.message.appended`:

```
type     bloodbank.v1.conversation.message.appended
subject  bloodbank.evt.v1.conversation.message.appended   # event
```

Example for `agent.invocation.start` command:

```
type     bloodbank.v1.agent.invocation.start
subject  bloodbank.cmd.v1.agent.invocation.start          # command
```

Repository maintenance and company reporting use these provider-neutral
event routes:

| CloudEvents `type`                              | NATS subject                                          |
| ----------------------------------------------- | ----------------------------------------------------- |
| `bloodbank.v1.repo.maintenance.started`         | `bloodbank.evt.v1.repo.maintenance.started`           |
| `bloodbank.v1.repo.maintenance.completed`       | `bloodbank.evt.v1.repo.maintenance.completed`         |
| `bloodbank.v1.repo.maintenance.failed`          | `bloodbank.evt.v1.repo.maintenance.failed`            |
| `bloodbank.v1.reporting.report.started`         | `bloodbank.evt.v1.reporting.report.started`           |
| `bloodbank.v1.reporting.report.completed`       | `bloodbank.evt.v1.reporting.report.completed`         |
| `bloodbank.v1.reporting.report.failed`          | `bloodbank.evt.v1.reporting.report.failed`            |

These lifecycle events use strict, privacy-preserving telemetry. Maintenance
failures identify a structured phase and code. Setup and preflight failures
set provider fields to `null`; provider and merge failures use schema branches
that require only the fields valid for that phase. Completed report coverage
lists each section and its state instead of publishing independent counters
that can contradict one another.

Report artifacts use opaque IDs such as `report:2026-07-15:json`, never raw
filesystem paths. Delivery metadata uses a configured logical
`destination_alias`, never a chat ID, user ID, phone number, or webhook URL.
Failure summaries are redacted, limited to 500 characters, and marked with
`redacted: true`. Producers MUST NOT publish stderr dumps, credentials,
credential-bearing URLs, access tokens, or absolute filesystem paths in these
events.

Canonical PM->agent dispatch contract:

- Command envelope type: `bloodbank.v1.agent.invocation.start`
- Command subject: `bloodbank.cmd.v1.agent.invocation.start`
- Target routing key: `data.target_agent_id`

Routers and consumers MUST dispatch this command by `data.target_agent_id`
rather than encoding the target agent in the subject path.

The legacy `event.>` / `command.>` / `reply.>` subject prefixes are
**deprecated** and will be removed when the migration completes (§16).

---

## 4. Envelope `kind` discriminator

The CloudEvents envelope carries a top-level `kind` field, distinct from
CloudEvents `type`:

```json
{ "kind": "event"   }   // immutable, fan-out, retained 7d
{ "kind": "command" }   // single-consumer, workqueue, retained 1d
{ "kind": "reply"   }   // short-lived correlation; not source of truth
```

Per ChatGPT Discussion 2: commands carry `delivery: "single_consumer"`,
`command_id`, and `idempotency_key`. Events carry `correlationid`,
`causationid`, and `ordering_key`. Both inherit the rest of the CloudEvents
1.0 envelope.

---

## 5. Action tense — events vs commands

| Envelope `kind` | Action tense                        | Examples                                                                                             |
| --------------- | ----------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `event`         | past tense / past-participle        | `created`, `started`, `completed`, `appended`, `spawned`, `received`, `failed`, `canceled`, `denied` |
| `command`       | imperative present                  | `create`, `start`, `complete`, `append`, `spawn`, `send`, `cancel`, `deny`, `invoke`                 |
| `reply`         | same form as the command it answers | mirrors the command's action verb                                                                    |

Mixing tenses across kind is a contract violation. The validator MUST reject
a `kind=event` envelope whose action is in §8.2 (imperative) and a
`kind=command` envelope whose action is in §8.1 (past).

`response` and `request` are **not** valid actions — they are nouns. Use the
verb pair `received` / `sent` for protocol boundaries (e.g.
`llm.response.received`, `llm.request.sent`).

---

## 6. Domain allowlist (v1)

Segment 3 of `type` MUST be one of:

| Domain         | Meaning                                                            | Status   |
| -------------- | ------------------------------------------------------------------ | -------- |
| `conversation` | The durable user-facing thread of turns and messages.              | active   |
| `agent`        | Agent runtime lifecycle and orchestration of LLM invocations.      | active   |
| `llm`          | Protocol-boundary events with a model provider.                    | active   |
| `cli`          | Terminal/process-backed agent runtimes (stdin/stdout/stderr/exit). | active   |

| `system`       | Bloodbank platform health (heartbeats, dapr, nats, replay).        | active   |
| `audio`        | Audio capture lifecycle — inbox ingestion, transcription jobs.     | active   |
| `repo`         | Repo-scoped PM facts such as decisions, intake triage, and tasks.  | active   |
| `lifecycle`    | Finite development mission: status, roadmap, checkpoints, gates, blockers. | active   |
| `finance`      | Household finance facts from the tiller sync — accounts, transactions, recurring/zombie subscriptions, cashflow projection. | active   |
| `attendance`   | Timekeeping and clock-state transitions across work sessions.       | active   |
| `curator`      | Purpose-driven curation of a watched directory — classify, enrich, rename, and route incoming files (the `folder-curator` skill). | active   |
| `reporting`    | Company reporting runs, archives, and delivery outcomes.             | active   |
| `approval`     | Human-in-the-loop approval grants/denies.                          | reserved |
| `workspace`    | Working directory / git state mutations.                           | reserved |
| `workflow`     | Multi-step workflow orchestration.                                 | reserved |
| `memory`       | Persistent agent memory writes/reads.                              | reserved |

`reserved` means the domain is registered but not yet emitted. Producers
MUST NOT invent domains; add them to this table in a PR before emitting.

---

## 7. Entity allowlist (v1)

Segment 4 of `type` MUST be one of:

| Entity             | Domain pairing (typical) | Notes                                                       |
| ------------------ | ------------------------ | ----------------------------------------------------------- |
| `thread`           | `conversation`           | One durable conversation across many turns.                 |
| `turn`             | `conversation`           | One user-prompt-to-final-response unit inside a thread.     |
| `message`          | `conversation`           | A single user/assistant message appended to the transcript. |
| `invocation`       | `agent`                  | One round of agent runtime calling an LLM.                  |
| `session`          | `cli`                    | One CLI session (e.g. `claude` or `copilot` process tree).  |
| `process`          | `cli`                    | An OS process spawned within a CLI session.                 |
| `stdout`           | `cli`                    | A stdout chunk emitted by a CLI process.                    |
| `stderr`           | `cli`                    | A stderr chunk emitted by a CLI process.                    |
| `request`          | `llm`                    | A request sent to an LLM provider.                          |
| `response`         | `llm`                    | A response received from an LLM provider.                   |
| `tool`             | `agent`                  | A tool-use action performed by an agent or subagent.        |
| `heartbeat`        | `system`                 | Liveness/health beat.                                       |
| `decision`         | `repo`                   | PM decision recorded for a repo; repo slug lives in data.    |
| `intake`           | `repo`                   | Incoming repo request triaged; repo slug lives in data.      |
| `task`             | `repo`                   | Repo work item created; repo slug lives in data.             |
| `maintenance`      | `repo`                   | Automated repository maintenance run and merge-gate outcome. |
| `file`             | `audio`, `curator`       | An on-disk artifact observed by a watcher — an audio inbox file, or a file arriving in a curated directory. |
| `transcription`    | `audio`                  | A speech-to-text job over a single audio file.              |
| `approval_request` | `approval` (reserved)    | Human approval prompt issued.                               |
| `worktree`         | `workspace` (reserved)   | Git worktree lifecycle.                                     |
| `branch`           | `workspace` (reserved)   | Git branch state changes.                                   |
| `diff`             | `workspace` (reserved)   | Captured diff artifact.                                     |
| `lifecycle`        | `lifecycle`              | Finite development mission envelope.                        |
| `mission`          | `lifecycle`              | A single lifecycle instance (status, roadmap, checkpoints). |
| `checkpoint`       | `lifecycle`              | A roadmap milestone that can be reached.                    |
| `gate`             | `lifecycle`              | An intentional pause or review point on a lifecycle.        |
| `roadmap`          | `lifecycle`              | A versioned plan of phases and checkpoints.                 |
| `status`           | `lifecycle`              | Aggregate lifecycle status and health.                      |
| `sync`             | `finance`                | One sheet→Postgres sync run (tiller).                       |
| `account`          | `finance`                | A tracked bank/credit/manual account.                       |
| `transaction`      | `finance`                | A posted transaction on an account.                         |
| `subscription`     | `finance`                | A recurring charge/income series (detected or curated).     |
| `zombie_charge`    | `finance`                | A charge on a series the owner already canceled.            |
| `paycheck`         | `finance`                | A recognized income deposit.                                |
| `projection`       | `finance`                | The liquid cashflow projection (breaches, troughs).         |
| `clock`            | `attendance`             | A time-clock integration session or state transition.       |
| `report`           | `reporting`              | One company report run, archive, and delivery lifecycle.     |

Entity additions follow the same PR-first rule as domains. A domain may not
emit an entity not paired with it here.

---

## 8. Action allowlists (v1)

### 8.1 Immutable event actions (past tense / past participle)

`created`, `resumed`, `started`, `ended`, `completed`, `failed`, `canceled`,
`generated`, `appended`, `received`, `sent`, `granted`, `denied`, `opened`,
`closed`, `spawned`, `exited`, `checked_out`, `requested`, `invoked`,
`recorded`, `triaged`, `updated`, `reached`, `resolved`, `detected`,
`flagged`, `routed`, `breached`, `clocked_in`, `clocked_out`.

### 8.2 Command actions (imperative present)

`create`, `resume`, `start`, `end`, `complete`, `fail`, `cancel`, `generate`,
`append`, `receive`, `send`, `grant`, `deny`, `open`, `close`, `spawn`,
`kill`, `checkout`, `invoke`, `request`, `toggle`, `clock_in`, `clock_out`.

Pairing across kinds is by semantic intent, not lexical: command `start`
yields event `started`; command `kill` yields event `exited`; command
`checkout` yields event `checked_out`. The validator does not enforce the
pairing — schema review does.

---

## 9. Banned tokens in `type`

The following tokens MUST NOT appear in any segment of `type`:

```
claude, anthropic, copilot, github, openai, gemini,
cursor, opencode, amazonq, codex, ollama, llama, mistral
```

Add to this list in a PR if a new provider integration appears. The
validator's check is `tokens(type) ∩ banned_tokens == ∅`.

Also forbidden in `type`:

- The literal words `response`, `request` as the `action` segment (use the
  verb pair `received` / `sent`; the noun form `response` / `request` lives
  in the `entity` segment).
- Any thread/turn/session/process **ID** (IDs live in `subject` and `data`).

---

## 10. Where provider / CLI / model identity lives

Provider, CLI tool, and model name go in the envelope's `actor` extension
object:

```json
{
  "actor": {
    "type": "agent_cli",
    "agent_id": "bloodbank.agent.claude",
    "cli": "claude",
    "provider": "anthropic",
    "model": "claude-sonnet-4.5"
  }
}
```

```json
{
  "actor": {
    "type": "agent_cli",
    "agent_id": "bloodbank.agent.copilot",
    "cli": "copilot",
    "provider": "github_copilot",
    "model": null
  }
}
```

`actor.type` is open-ended (`agent_cli`, `agent_api`, `operator`, `service`,
`scheduler`). `actor.provider` and `actor.cli` are free-form strings — the
banned-token rule applies to `type`, NOT to `actor.*`.

The existing CloudEvents `producer`, `service`, `domain`, and `source`
fields continue to apply per `cloudevent_base.v1.json`. Conceptually:

- `source` — origin URI of the producer (e.g. `urn:33god:agent:claude-code`).
  Provider names are allowed here because `source` is a free-form URI.
- `producer` — canonical producer name (e.g. `claude-code`, `copilot-cli`).
- `actor` — who/what generated this fact, normalized for downstream agnostic
  consumption.

---

## 11. Required envelope fields beyond the CloudEvents base

In addition to everything required by `bloodbank/schemas/_common/cloudevent_base.v1.json`:

| Field             | Applies to            | Required? | Notes                                                                         |
| ----------------- | --------------------- | --------- | ----------------------------------------------------------------------------- |
| `kind`            | event, command, reply | yes       | One of `event`, `command`, `reply`. Subject kind marker MUST match.           |
| `actor`           | event, command        | yes       | Object per §10. Replies inherit the actor of the command they answer.         |
| `ordering_key`    | event                 | yes       | Stable string ordering bucket. See §11.1.                                     |
| `command_id`      | command               | yes       | Unique command identifier. Same value across retries.                         |
| `idempotency_key` | command               | yes       | Stable key for the command's effect. See §11.2.                               |
| `delivery`        | command               | yes       | Always `single_consumer` for v1.                                              |
| `correlationid`   | event, command, reply | yes       | Already required by base. For commands, equals `command_id` when root-issued. |
| `causationid`     | event, command, reply | yes       | Already required by base. For replies, equals the originating `command_id`.   |

Schema validation enforces JSON Schema `date` and `date-time` formats. Invalid
calendar dates and non-RFC 3339 timestamps fail validation; the `format`
keyword is not documentation-only.

### 11.1 `ordering_key` rules

`ordering_key` is a deterministic string that places this event into a total
order with siblings on the same logical entity. Convention:

```
thread:<thread_id>
turn:<turn_id>
invocation:<invocation_id>
session:<session_id>             # agent CLI session (was cli_session)
process:<process_id>
transcription:<transcription_id>
file:<sha256(file_path)|file_id>
sync:<run_id>                    # finance: one tiller sync run
account:<account_id>             # finance: per-account transaction/paycheck order
transaction:<txn_id>
subscription:<series_id>         # finance: recurring-series lifecycle incl. zombie strikes
projection:liquid                # finance: single household-wide projection bucket
clock:<clock_system>:<principal> # attendance: one worker/system time-clock state bucket
```

Pick the narrowest bucket that captures the event's natural ordering.
A `conversation.message.appended` uses `turn:<turn_id>`. A
`cli.stdout.appended` uses `process:<process_id>`. An
`audio.transcription.completed` uses `transcription:<transcription_id>`;
an `audio.file.received` uses `file:<sha256(file_path)>` so re-detections
of the same artifact form a stable bucket.

### 11.2 `idempotency_key` rules

For commands, idempotency is `<action>:<entity-scope>`:

```
agent.invocation.start : thread:<thread_id>:turn:<turn_id>
cli.process.spawn      : cli_session:<session_id>:cmd:<sha256(args)>
agent.tool.invoke      : invocation:<invocation_id>:tool_call:<id>
```

A retry of the same command MUST present the same `idempotency_key`. Bloodbank
deduplicates on `(idempotency_key, command_id)` before forwarding.

---

## 12. Schema directory layout

Bloodbank v1 schemas live under `bloodbank/schemas/` in this repo:

```
bloodbank/schemas/
  _common/
    cloudevent_base.v1.json
    types.v1.json
  bloodbank/v1/
    conversation/
      thread.created.v1.json
      thread.resumed.v1.json
      turn.started.v1.json
      turn.completed.v1.json
      message.appended.v1.json
    agent/
      invocation.start.v1.json
      invocation.started.v1.json
      invocation.completed.v1.json
      invocation.failed.v1.json
    llm/
      request.sent.v1.json
      response.received.v1.json
    cli/
      session.started.v1.json
      session.ended.v1.json
      process.spawned.v1.json
      process.exited.v1.json
      stdout.appended.v1.json
      stderr.appended.v1.json
    agent/
      tool.requested.v1.json
      tool.invoked.v1.json
      tool.completed.v1.json
    system/
      heartbeat.received.v1.json
    repo/
      maintenance.started.v1.json
      maintenance.completed.v1.json
      maintenance.failed.v1.json
    reporting/
      report.started.v1.json
      report.completed.v1.json
      report.failed.v1.json
```

Each schema:

- `$id` MUST be `https://33god.dev/schemas/bloodbank/v1/<domain>/<entity>.<action>.v1.json`.
- MUST `$ref` `../../../_common/cloudevent_base.v1.json`.
- MUST set `properties.type.const` to the full 5-token type string.
- MUST set `properties.kind.const` to `event` or `command`.
- MUST set `properties.domain.const` to match segment 3 of `type`.

There is no provider-named subdirectory (no
`bloodbank/schemas/bloodbank/v1/copilot/`) — provider identity does not
shape the schema tree.

Runtime validator lookup (`services/agent-hooks/core/validate.py`) resolves
the schema root in this order:

1. `BLOODBANK_SCHEMAS_DIR` env var (canonical override).
2. `HOLYFIELDS_SCHEMAS_DIR` env var (backward-compat override).
3. Walk up from the source file to a directory containing both
   `docs/event-naming.md` and `schemas/` (this repo).
4. Sibling `holyfields/schemas/` (transitional fallback).
5. `$HOME/code/33GOD/bloodbank/schemas` / `holyfields/schemas` final fallback.

Run `mise run validate:schemas` to confirm the tree is internally
consistent (every `$id` unique, every `$ref` resolves).

---

## 13. `dataschema`, `schemaref`, and Apicurio keys

Following the new shape:

- `dataschema` — `apicurio://holyfields/bloodbank.v1.<domain>.<entity>.<action>/versions/<n>`
- `schemaref` — `bloodbank.v1.<domain>.<entity>.<action>.v1` (string)

The Apicurio registry's artifact ID is the 5-token type string, not the
filesystem path. Holyfields' registration script (`scripts/register-schemas.sh`
or equivalent) uses the type as the key when uploading.

---

## 14. Canonical event sequence for an agent turn

For a single agent turn from any CLI (Claude, Copilot, Codex, future), the
normalized event sequence is:

```
bloodbank.v1.conversation.thread.created         # only on first turn of a thread
                                                  # OR
bloodbank.v1.conversation.thread.resumed         # if a known thread is reopened
bloodbank.v1.conversation.turn.started
bloodbank.v1.agent.invocation.started
bloodbank.v1.agent.session.started               # agent CLI session (supersedes cli.session.started)
bloodbank.v1.cli.process.spawned                 # CLI-backed paths only
bloodbank.v1.cli.stdout.appended                 # 0..n; chunked
bloodbank.v1.cli.stderr.appended                 # 0..n; chunked
bloodbank.v1.llm.request.sent                    # protocol boundary
bloodbank.v1.llm.response.received               # protocol boundary
bloodbank.v1.agent.tool.requested            # 0..n
bloodbank.v1.agent.tool.invoked              # 0..n
bloodbank.v1.agent.tool.completed            # 0..n
bloodbank.v1.conversation.message.appended       # durable transcript record
bloodbank.v1.agent.invocation.completed
bloodbank.v1.conversation.turn.completed
```

Claude, Copilot, and Codex adapters MUST emit the same sequence; only
`actor.*` and payload details differ.

---

## 15. Migration map — legacy → v1 type renames

The hard-rename (no aliases) list. As of 2026-06-07 the agent-hooks mapping is
no longer hand-maintained in each `publish.py`: it is propagated from the single
source of truth `services/agent-hooks/hooks.master.json` by `sync.py`
(`mise run hooks:sync`). Divergence resolutions are recorded in
`services/agent-hooks/hooks.mappings.lock.json`.

Two resolutions landed in this revision (see the lock for rationale):

- **`cli.session.*` → `agent.session.*`** — agent CLI session events moved
  under the `agent` domain (beside `agent.invocation` / `agent.tool`). The
  `cli` domain keeps `process` / `stdout` / `stderr`. Ordering bucket
  `cli_session` → `session`.
- **post-tool hooks emit `agent.tool.completed`** (not `agent.tool.invoked`)
  on every agent — the single post-tool hook fires after execution and
  carries `outcome`.

| Legacy `type`              | v1 `type`                                 | Notes                                                               |
| -------------------------- | ----------------------------------------- | ------------------------------------------------------------------- |
| `agent.session.started`    | `bloodbank.v1.agent.session.started`        | Plus `bloodbank.v1.conversation.thread.created` on first turn.      |
| `agent.session.ended`      | `bloodbank.v1.agent.session.ended`          | Plus `bloodbank.v1.conversation.turn.completed` if a turn was open. |
| `agent.prompt.submitted`   | `bloodbank.v1.conversation.turn.started`  | The prompt is what starts a turn.                                   |
| `agent.tool.requested`     | `bloodbank.v1.agent.tool.requested`   |                                                                     |
| `agent.tool.invoked`       | `bloodbank.v1.agent.tool.invoked`     |                                                                     |
| `agent.subagent.completed` | `bloodbank.v1.agent.invocation.completed` | Sub-agent runs are nested invocations.                              |
| `agent.subagent.started`   | `bloodbank.v1.agent.invocation.started`   | (openclaw emits this)                                               |
| `copilot.session.started`  | `bloodbank.v1.agent.session.started`        | `actor.cli=copilot`, `actor.provider=github_copilot`.               |
| `copilot.session.ended`    | `bloodbank.v1.agent.session.ended`          |                                                                     |
| `copilot.prompt.submitted` | `bloodbank.v1.conversation.turn.started`  |                                                                     |
| `copilot.tool.pre`         | `bloodbank.v1.agent.tool.requested`   |                                                                     |
| `copilot.tool.post`        | `bloodbank.v1.agent.tool.completed`   |                                                                     |
| `copilot.error.occurred`   | `bloodbank.v1.agent.invocation.failed`    |                                                                     |
| `copilot.agent.stopped`    | `bloodbank.v1.agent.invocation.completed` |                                                                     |
| `codex.session.started`    | `bloodbank.v1.agent.session.started`        | `actor.cli=codex`, `actor.provider=openai`.                         |
| `codex.session.ended`      | `bloodbank.v1.agent.session.ended`          |                                                                     |
| `codex.prompt.submitted`   | `bloodbank.v1.conversation.turn.started`  |                                                                     |
| `codex.tool.pre`           | `bloodbank.v1.agent.tool.requested`       |                                                                     |
| `codex.tool.post`          | `bloodbank.v1.agent.tool.completed`       |                                                                     |
| `codex.subagent.started`   | `bloodbank.v1.agent.invocation.started`   |                                                                     |
| `codex.subagent.stopped`   | `bloodbank.v1.agent.invocation.completed` |                                                                     |
| `smoketest.ping`           | `bloodbank.v1.system.heartbeat.received`  | Smoke fixture.                                                      |

Anything not on this table that does not match §2's regex MUST be
quarantined by the publisher (§16, follow-up T-4).

---

## 16. Migration status

Implementation landed alongside the contract. The §15 rename and the
schema/publisher/stream cutover happened in a single hard-rename PR — no
aliases, no deprecation period.

| ID   | Repo       | Work                                                                                                                                                            | Status                                  |
| ---- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- |
| T-1  | holyfields | Tighten `_common/cloudevent_base.v1.json` `type` regex to §2's regex. Add `kind`, `actor`, `ordering_key` fields.                                               | DONE                                    |
| T-2  | bloodbank  | Schema tree at `bloodbank/schemas/bloodbank/v1/<domain>/...` per §12 + `_common/{cloudevent_base,types}.v1.json` deps. Validator and CI consume it locally.     | DONE                                    |
| T-3  | bloodbank  | Port Pydantic + Zod generators from Holyfields into `bloodbank/scripts` + `bloodbank/tools/generators` and emit `BloodbankV1Type` enum from the local tree.     | OPEN — see RECOMMENDATION.md            |
| T-4  | bloodbank  | `services/agent-hooks/core/{envelope,validate}.py` enforce §2, §3, §5, §9 and §11 on every envelope. Loud `ContractViolation`; no quarantine.                   | DONE                                    |
| T-5  | bloodbank  | `services/agent-hooks/{claude,copilot,codex,openclaw}/publish.py` (and `watch.py`) emit v1 types per §15.                                                       | DONE                                    |
| T-6  | bloodbank  | `compose/nats/streams.json` filters are `bloodbank.evt.v1.>` and `bloodbank.{cmd,rpy}.v1.>`. `compose/docker-compose.yml` env defaults migrated.                | DONE                                    |
| T-7  | bloodbank  | `services/event-toaster/main.py` subscribes to `bloodbank.evt.v1.>`. (ntfy formatter is operator-local — out of scope per goal.)                                | PARTIAL — subject default migrated      |
| T-8  | bloodbank  | `cli/bb.py verify-envelope` runs the full v1 contract against any envelope on stdin/file.                                                                       | DONE                                    |
| T-9  | bloodbank  | `ops/smoketest/smoketest-bloodbank-naming.sh` + `mise run smoketest:bloodbank-naming` — stdlib verifier (no Docker) for §14 sequence × {claude, copilot, codex}. | DONE                                    |
| T-10 | bloodbank  | `compose/nats/README.md`, `services/agent-hooks/README.md`, `ops/smoketest/README.md`, `AGENTS.md` all point here.                                              | DONE                                    |
| T-11 | 33god meta | If ADR-0001 needs an amendment recording this contract, file ADR-0002.                                                                                          | OPEN — metarepo-side                    |

### 16.2 Verifier checks (T-9)

The smoke harness asserts:

```
type matches §2 regex
type has exactly 5 dot-separated tokens
tokens[0] == "bloodbank"
tokens[1] matches v[0-9]+
tokens[2] in §6 domain allowlist
tokens[3] in §7 entity allowlist
tokens[4] in §8.1 for kind=event, §8.2 for kind=command
tokens ∩ §9 banned tokens == ∅
subject == "bloodbank." + {evt|cmd|rpy} + ".v1." + tokens[2..5].join(".")
subject's kind marker matches envelope.kind
source, actor, subject, correlationid, ordering_key all present on every event
actor.cli ∈ {claude, copilot, codex, ...} per actual emitter
ordering_key is stable across events on the same entity
```

---

## 17. References

- [CloudEvents 1.0 spec](https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md) — `type`, `source`, `subject`, duplicate detection via `source + id`.
- [NATS subject-based messaging](https://docs.nats.io/nats-concepts/subjects) — dot-hierarchy guidance.
- Source directives: `~/d/Inbox/Bloodbank ChatGPT Discussion 2.md` (2026-05-14).
- Repo conventions: `bloodbank/CLAUDE.md`, `bloodbank/compose/nats/streams.json`.

---
