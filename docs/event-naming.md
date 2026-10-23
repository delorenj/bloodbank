# Bloodbank Event Naming Contract

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

Every event's CloudEvents `type` is exactly four dotted tokens:

```
bloodbank.<domain>.<entity>.<action>
```

The vendor prefix (`bloodbank`) is baked into the type so a consumer reading a
single envelope knows which contract the fact belongs to without parsing
`dataschema`. There is no contract version token; §3.1 states the rule that
replaced it.

Regex (anchored, lowercase, underscores allowed in body tokens):

```
^bloodbank\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$
```

Valid:

```
bloodbank.conversation.message.appended
bloodbank.llm.response.received
bloodbank.cli.stdout.appended
bloodbank.agent.invocation.started
bloodbank.repo.maintenance.failed
```

Invalid (contract violation):

```
agent.session.started                  # legacy 3-token shape; no vendor prefix
bloodbank.v1.agent.session.started     # version token; segment 2 is the domain
bloodbank.agent.claude.response        # provider name in type
bloodbank.copilot.message.generated    # provider name in type
bloodbank.agent.session.response       # response is not an action verb
bloodbank.conversation.message         # only 3 tokens
bloodbank.Agent.session.started        # uppercase forbidden
```

Token rules:

- `bloodbank` is literal segment 1.
- `domain` (segment 2) MUST be in §6 allowlist.
- `entity` (segment 3) MUST be in §7 allowlist.
- `action` (segment 4) MUST be in §8 allowlist and MUST match the tense
  required by §5 for the envelope `kind`.
- Every body token is `[a-z][a-z0-9_]*` (lowercase, may contain underscores,
  must start with a letter). Hyphens are not allowed.

---

## 3. NATS subject — shape, kind marker, stream mapping

NATS subject mirrors `type` but inserts a **kind marker** as the second
segment so JetStream streams can be bound by transport-level routing
without parsing the envelope body:

```
bloodbank.<kind>.<domain>.<entity>.<action>
```

`<kind>` ∈ `{ evt, cmd, rpy }`:

| Marker | Envelope `kind` | Stream               | Subject filter    |
| ------ | --------------- | -------------------- | ----------------- |
| `evt`  | `event`         | `BLOODBANK_EVENTS`   | `bloodbank.evt.>` |
| `cmd`  | `command`       | `BLOODBANK_COMMANDS` | `bloodbank.cmd.>` |
| `rpy`  | `reply`         | `BLOODBANK_COMMANDS` | `bloodbank.rpy.>` |

Subject is 5 tokens. `type` stays 4 tokens. The subject's kind marker is a
**transport-only** redundancy with the envelope `kind` field — consumers
MUST treat `envelope.kind` as authoritative; the subject marker exists only
so NATS can route without deserializing.

The pair `(domain, entity, action)` is identical across subject and type.

Example for `conversation.message.appended`:

```
type     bloodbank.conversation.message.appended
subject  bloodbank.evt.conversation.message.appended   # event
```

Example for `agent.invocation.start` command:

```
type     bloodbank.agent.invocation.start
subject  bloodbank.cmd.agent.invocation.start          # command
```

Repository maintenance and company reporting use these provider-neutral
event routes:

| CloudEvents `type`                     | NATS subject                                 |
| -------------------------------------- | -------------------------------------------- |
| `bloodbank.repo.maintenance.started`   | `bloodbank.evt.repo.maintenance.started`     |
| `bloodbank.repo.maintenance.completed` | `bloodbank.evt.repo.maintenance.completed`   |
| `bloodbank.repo.maintenance.failed`    | `bloodbank.evt.repo.maintenance.failed`      |
| `bloodbank.reporting.report.started`   | `bloodbank.evt.reporting.report.started`     |
| `bloodbank.reporting.report.completed` | `bloodbank.evt.reporting.report.completed`   |
| `bloodbank.reporting.report.failed`    | `bloodbank.evt.reporting.report.failed`      |
| `bloodbank.reporting.journal.received`  | `bloodbank.evt.reporting.journal.received`   |
| `bloodbank.reporting.incident.observed` | `bloodbank.evt.reporting.incident.observed`  |

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

### 3.1 The subject is an address, not a version

A subject names *where a fact is delivered*. It does not carry a contract
version, and the five-token head above is fixed for the life of the system.
This is the rule that replaced versioning, and it is the whole of it:

- **A breaking payload change gets a new `action`, or a new `entity` — never
  a version segment.** If the fact genuinely changed, it is a different fact
  and deserves its own name. If it did not change, the address must not move
  underneath the consumers bound to it.
- **Payload compatibility lives in `dataschema` (§13), not in the subject.**
  The schema revision is a separate axis that evolves independently and is
  read from the envelope, not from the routing layer.
- **Therefore `bloodbank.evt.>` is all any catch-all consumer ever needs to
  bind**, and a durable's `filter_subject` is stable enough to be asserted
  against a compiled constant.

The two axes are easy to confuse because both once used a `v`. They are not
the same thing, and only one of them still exists:

```
type        bloodbank.repo.maintenance.failed                              # the fact — no version
dataschema  apicurio://holyfields/bloodbank.repo.maintenance.failed/versions/2   # the schema revision
schemaref   bloodbank.repo.maintenance.failed.v2                          # the same revision, short form
```

The trailing `/versions/<n>` and `.v<n>` are the **schema revision**. They are
expected to increment, they say nothing about the subject, and nothing about
them is deprecated by the removal of the contract version.

### 3.2 Command dispatch and registry eligibility

Canonical PM->agent dispatch contract:

- Command envelope type: `bloodbank.agent.invocation.start`
- Command subject: `bloodbank.cmd.agent.invocation.start`
- Target routing key: `data.target_agent_id`

Routers and consumers MUST dispatch this command by `data.target_agent_id`
rather than encoding the target agent in the subject path.

**Two delivery lanes.** The same data payload travels on two command types, each
with its own durable consumer on `BLOODBANK_COMMANDS` and its own in-flight
slots, so interactive work never queues behind batch work:

| type | subject | meaning |
| --- | --- | --- |
| `bloodbank.agent.invocation.start` | `bloodbank.cmd.agent.invocation.start` | Batch lane. Unattended or scheduled work: ticket grooming, delegation, PM fan-out. FIFO behind other batch work. Durable `bloodbank-hermes-gateway`, 4 slots. |
| `bloodbank.agent.invocation.invoke` | `bloodbank.cmd.agent.invocation.invoke` | Interactive lane. A human is waiting on the answer (a voice memo, a chat nudge). Durable `bloodbank-hermes-gateway-interactive`, 2 reserved slots. |

`invoke` is for commands where a human is waiting, nothing else. The gateway
still emits `agent.invocation.started/completed/failed` for both (not
`agent.invocation.invoked`; Section 8.2 pairing is by review, not enforced),
stamping `data.lane` (`batch` | `interactive`) on every lifecycle fact. Design:
`docs/interactive-command-lane.md`.

The fleet-shared Hermes consumer treats registry routing as an explicit
eligibility contract. The registry root must be a mapping with exact integer
`schema_version: 1` and an explicit `agents` mapping. Empty/missing documents,
unsupported versions, non-string agent keys, and non-mapping agent records are
transient registry failures; consumers must not skip ambiguous records and then
fall through to direct-profile routing. Agent keys must be canonical lowercase
slugs matching `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`; whitespace-padded,
case-variant, or otherwise invalid keys make the entire snapshot transiently
invalid and are never normalized. A structurally valid registry record is
routable only when `profile_name` is nonblank and `bloodbank` is a mapping whose
`gateway_scope` is exactly `fleet` and whose `target_agent_id` exactly equals
the registry `agent_id`. Activation defaults to ALLOW: an absent
`bloodbank.enabled` means enabled, the YAML boolean `true` means enabled, and
only an explicit `false` quarantines the agent. A present `enabled` that is not
a YAML boolean (`"true"`, `null`, `1`) is invalid; the consumer treats the row
as disabled and logs an ERROR naming `agents.<id>.bloodbank.enabled`. A missing
`bloodbank` mapping, an explicit `false`, an invalid `enabled`, or a
mismatched scope/target is a terminal not-routable result.

Eligibility is re-read after durable claim and immediately before dispatch. If
it becomes invalid after either started event may have escaped, the consumer
must durably store and publish the existing canonical
`agent.invocation.failed` -> `conversation.turn.completed` closure before
terminally acknowledging the command. Failed closure persistence/publication is
retryable, and exact redelivery replays the stored envelopes without executing
the agent. Only a journal-proven pre-start rejection may be eventless. Explicit
adapter `target_profiles` entries are separate, operator-owned static overrides
and do not inherit registry eligibility.

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

## 6. Domain allowlist

Segment 3 of `type` MUST be one of:

| Domain         | Meaning                                                            | Status   |
| -------------- | ------------------------------------------------------------------ | -------- |
| `conversation` | The durable user-facing thread of turns and messages.              | active   |
| `agent`        | Agent runtime lifecycle and orchestration of LLM invocations.      | active   |
| `llm`          | Protocol-boundary events with a model provider.                    | active   |
| `cli`          | Terminal/process-backed agent runtimes (stdin/stdout/stderr/exit). | active   |

| `system`       | Bloodbank platform health (heartbeats, dapr, nats, replay).        | active   |
| `audio`        | Audio capture lifecycle — inbox ingestion, transcription jobs, intake extracted from transcripts. | active   |
| `repo`         | Repo-scoped PM facts such as decisions, intake triage, and tasks.  | active   |
| `lifecycle`    | Finite development mission: status, roadmap, checkpoints, gates, blockers. | active   |
| `finance`      | Household finance facts from the tiller sync — accounts, transactions, recurring/zombie subscriptions, cashflow projection. | active   |
| `billing`      | Project cost snapshots and their immutable revisions; customer statements requested from the Farm and generated by the billing authority. | active |
| `attendance`   | Timekeeping and clock-state transitions across work sessions.       | active   |
| `curator`      | Purpose-driven curation of a watched directory — classify, enrich, rename, and route incoming files (the `folder-curator` skill). | active   |
| `reporting`    | Company reporting runs, archives, and delivery outcomes.             | active   |
| `portfolio`    | Company-level intake, delegation receipts, approvals, escalations, and capacity facts. | active   |
| `project`      | Project-scoped facts keyed by the pjangler project slug: periodic activity reports, deployments of built artifacts, and other whole-project outcomes that span a project's repos and boards. | active   |
| `telephony`    | Phone calls on the operator's lines (Telnyx numbers and the forwarded personal cell): calls, recordings, voicemail transcripts, and call commands executed through Telnyx. Text messages are `messaging` (one entity per transport: `sms`, `mms`, `rcs`, `imessage`). | active   |
| `messaging`    | Human messages and platform alerts on every channel — email, chat platforms, messaging apps, and carrier texts (SMS/MMS/RCS/iMessage). The entity segment names the platform (`messaging.<platform>.<action>`: `received` for a message, `notified` for a mention/reaction/activity alert); see the platform rows in §7. | active   |
| `crank`        | Desk-mode crank sessions: the operator's circuit of agent tabs in the zellij Workspace and Wingman, the voice pair programmer that watches it (`duty.started`, `duty.ended`, `alert.raised`). | active   |
| `approval`     | Human-in-the-loop approval grants/denies.                          | reserved |
| `workspace`    | Working directory / git state mutations.                           | reserved |
| `workflow`     | Multi-step workflow orchestration: scheduled agent workflows such as the gateway's daily free-provider scout (`account.expired`, `task.completed`, `task.failed`). | active   |
| `memory`       | Persistent agent memory writes/reads.                              | reserved |

`reserved` means the domain is registered but not yet emitted. Producers
MUST NOT invent domains; add them to this table in a PR before emitting.

---

## 7. Entity allowlist

Segment 4 of `type` MUST be one of:

| Entity             | Domain pairing (typical) | Notes                                                       |
| ------------------ | ------------------------ | ----------------------------------------------------------- |
| `cost`             | `billing`                | Scoped provider bill-period snapshot, replaced by revisions. |
| `invoice`          | `billing`                | One customer statement requested from the Farm and generated (planned or issued) by the billing authority; the client and period live in data. |
| `thread`           | `conversation`           | One durable agent conversation across many turns — the original model of an agent session. `thread:<thread_id>` is the ordering bucket for `conversation.turn.*`; `thread.created`/`thread.resumed` have schemas but no producer emits them. Not used by `messaging` (there a thread is `data.thread_id`). |
| `turn`             | `conversation`           | One user-prompt-to-final-response unit inside a thread.     |
| `message`          | `conversation`           | A single user/assistant message appended to the transcript. (Retired in `telephony` and `messaging`: texts and chat messages are per-transport/per-platform `messaging` entities.) |
| `email`            | `messaging`              | The email platform: `messaging.email.received` (one message), `messaging.email.notified` (one alert). |
| `telegram`         | `messaging`              | The Telegram platform: `messaging.telegram.received` (one message), `messaging.telegram.notified` (one alert). |
| `slack`            | `messaging`              | The Slack platform: `messaging.slack.received` (one message), `messaging.slack.notified` (one alert). |
| `teams`            | `messaging`              | The Microsoft Teams platform: `messaging.teams.received` (one message), `messaging.teams.notified` (one alert). |
| `whatsapp`         | `messaging`              | The WhatsApp platform: `messaging.whatsapp.received` (one message), `messaging.whatsapp.notified` (one alert). |
| `google_chat`      | `messaging`              | The Google Chat platform: `messaging.google_chat.received` (one message), `messaging.google_chat.notified` (one alert). |
| `sms`              | `messaging`              | Carrier SMS: `messaging.sms.{received,sent,completed,failed}` and the `messaging.sms.send` command (Telnyx). |
| `mms`              | `messaging`              | Carrier MMS (media): `messaging.mms.{received,sent,completed,failed}` and the `messaging.mms.send` command (Telnyx). |
| `rcs`              | `messaging`              | RCS chat through Google Messages / BlueBubbles: `messaging.rcs.{received,sent}`. |
| `imessage`         | `messaging`              | iMessage through BlueBubbles: `messaging.imessage.{received,sent}`. |
| `invocation`       | `agent`                  | One round of agent runtime calling an LLM. `started` -> `progressed` (0..n: an interim assistant message of the running turn, `data.seq` 1..n per command, `data.text` <= 500 chars; the Hermes gateway publishes every non-blank one, consumers throttle) -> `detached` (0..1 per attempt: the turn outlived its interactive lane's wall clock and gave its slot back; it runs on, and its terminal fact carries `detached: true`) -> `completed` or `failed`. |
| `session`          | `cli`                    | One CLI session (e.g. `claude` or `copilot` process tree).  |
| `process`          | `cli`                    | An OS process spawned within a CLI session.                 |
| `stdout`           | `cli`                    | A stdout chunk emitted by a CLI process.                    |
| `stderr`           | `cli`                    | A stderr chunk emitted by a CLI process.                    |
| `request`          | `llm`                    | A request sent to an LLM provider.                          |
| `response`         | `llm`                    | A response received from an LLM provider.                   |
| `usage`            | `llm`                    | One settled gateway request's normalized token and timing fact. |
| `allowance`        | `llm`                    | A provider-reported account/window utilization snapshot.    |
| `tool`             | `agent`                  | A tool-use action performed by an agent or subagent.        |
| `hook`             | `agent`, `system`        | Payload-free hook invocation revisions and hook-hub inventory/health observations. |
| `heartbeat`        | `system`                 | Liveness/health beat.                                       |
| `decision`         | `repo`                   | PM decision recorded for a repo; repo slug lives in data.    |
| `intake`           | `repo`, `portfolio`, `audio` | Incoming request awaiting PM triage. `repo`: triaged by a PM (repo slug in data). `portfolio`: Director intake. `audio`: one ticketable task extracted from a transcript (pjangler project in `data.project_id`); `repo.intake.triaged` closes the loop through the same `data.intake_id`. |
| `task`             | `repo`                   | Repo work item created; repo slug lives in data.             |
| `board`            | `repo`                   | Provider-neutral project board; repo slug and provider identity live in data. |
| `maintenance`      | `repo`                   | Automated repository maintenance run and merge-gate outcome. |
| `skill`            | `repo`                   | Repo-scoped agent skill mutation from an automated tuning cycle; repo slug and skill name live in data. |
| `file`             | `audio`, `curator`       | An on-disk artifact observed by a watcher — an audio inbox file, or a file arriving in a curated directory. |
| `transcription`    | `audio`, `telephony`     | A speech-to-text job over a single audio file, or over a call recording or voicemail. |
| `memo`             | `audio`                  | A short voice prompt captured on a device (Holocene Auto / Agent Pad) and routed to an agent. One memo is one journey: `uploaded` -> `received` -> `transcribed` -> `dispatched` -> `replied` -> `played` (the device reports playback), or `failed` at any stage. While the agent works, `narrated` (0..n: the watcher spoke a progress update for the car) and `notified` (0..n: the car's speech arbiter queued, started, ended, deferred, dropped or interrupted an acknowledgement, heard-echo, progress update or reply) annotate the journey; before `replied`, `voiced` (1..n: one sentence-chunk of a long reply rendered and uploaded as `replies/...-<memo_id>-c<ii>of<nn>.opus`, so the car starts speaking while the rest renders; a reply short enough for one file publishes one `voiced`, chunk 1 of 1, naming the reply file itself, when it is up; `replied` waits for the watcher's phone-or-car decision and still names the full file); after `replied`, `mirrored` (0..1: the reply's Earshot phone copy landed late, in the watcher's background retry, after `replied` had said it was left to the car); on the fast in-car conversation path (`MEMO_CONVERSE`), `routed` (1 per fast-model decision, between `transcribed` and `replied`: `conv_id`, `action`, answering model, rung, attempts, latency, tokens, or `fallback_reason` when the turn went direct to the agent) and `delegated` (1 per driver-confirmed brief, at dispatch: `tile_agent_id`, `target_agent_id`, `conv_id`, `brief_id`, `brief_sha256`, `command_id`, `lane`, `perm`). Every stage fact and the agent invocation command share `correlationid = data.memo_id`. |
| `earshot`          | `audio`                  | A sound file dropped in an Earshot bucket channel, as the phone plays it. One drop is one journey: `detected` -> `started` -> `ended`, or `failed` (could not play) or `skipped` (the phone saw it too late and did not play it). `data.phase` carries the device's own word (`finished`, `missed`). `correlationid = data.event_id`, `ordering_key = earshot:<event_id>`. |
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
| `journal`          | `reporting`              | Durable receipt for a developer journal entering its workflow. |
| `incident`         | `reporting`              | One dated occurrence of a tracked problem area.             |
| `work`             | `portfolio`              | A Director-delegated unit; child boards remain lifecycle authority. |
| `receipt`          | `portfolio`              | Retry-safe terminal receipt for one delegated work attempt.  |
| `approval`         | `portfolio`              | Human or policy decision boundary for delegated work.        |
| `escalation`       | `portfolio`              | Exception raised to and resolved by the Director.            |
| `capacity`         | `portfolio`              | Snapshot of global pipeline or derivative delegation slots.  |
| `lease`            | `portfolio`              | Time-bounded ownership of one global delegation slot.        |
| `activity`         | `project`                | One audience-specific activity report over a bounded window of a project's repos, boards and agent sessions. |
| `deployment`       | `project`                | One install of a project's built artifact on a target (phone, host service, site); version, commit, checksum and target live in data. |
| `artifact`         | `project`                | One durable user-facing deliverable of a project (diagram, chart, document, idea, BMAD artifact) captured into content-addressed storage before publication; digest, media type, revision and provenance live in data, never paths or bytes. |
| `duty`             | `crank`                  | One Wingman duty session: from going on duty to going off. `data.duty_id` is the session and the envelope `correlationid`. |
| `alert`            | `crank`                  | One line Wingman said to the operator about crank tabs (a tab waiting, finished, broken, waiting too long, or the focused tab's progress); the signals behind it live in data. |
| `call`             | `telephony`              | One phone call on an operator line, from setup to hangup; recordings and voicemails are `call.recorded`. |

Entity additions follow the same PR-first rule as domains. A domain may not
emit an entity not paired with it here.

**`messaging` entities are platforms, on purpose.** In `messaging` the entity
segment names the chat platform (`messaging.email.received`,
`messaging.telegram.notified`, …) rather than generic `message` /
`notification` entities with the platform in `data`. A chat platform is the addressable channel a message
travels on, not a compute provider: it is not a §9 banned token, and the §10
rule (provider/CLI/model identity lives in `actor`) is about who *produced* an
event, not where a human message was *sent*. Splitting by platform lets each
platform's schema grow its own payload (email headers, Slack blocks) and lets
subscribers bind to `bloodbank.evt.messaging.<platform>.>`, or to
`bloodbank.evt.messaging.*.received` / `.*.notified` across every platform.
The action says what happened: `received` is a message, `notified` is an alert
that isn't one (mention, reaction, activity ping). A new platform is a new
entity row here plus a `received` and a `notified` schema; there is no
`data.platform` field. Ordering is `messaging:<platform>:<thread_id>` (§11.1),
never the agent-conversation `thread:` bucket.

Carrier texts follow the same rule, with the transport as the entity:
`messaging.sms.*`, `messaging.mms.*`, `messaging.rcs.*`,
`messaging.imessage.*` (there is no `data.channel` field). They moved here
from `telephony.message.*`, which is retired; telephony keeps calls. Unlike
chat platforms, carrier texts order per operator line (`line:<line>`), so a
conversation that switches between SMS and MMS, or falls back from RCS to
SMS, stays in one bucket. `bloodbank.evt.messaging.*.received` therefore
covers every inbound human message, texts included.

---

## 8. Action allowlists

### 8.1 Immutable event actions (past tense / past participle)

`created`, `resumed`, `started`, `ended`, `completed`, `failed`, `canceled`,
`generated`, `appended`, `received`, `sent`, `granted`, `denied`, `opened`,
`closed`, `spawned`, `exited`, `checked_out`, `requested`, `invoked`,
`recorded`, `triaged`, `updated`, `reached`, `resolved`, `detected`,
`flagged`, `routed`, `breached`, `observed`, `clocked_in`, `clocked_out`.
`delegated`, `raised`, `released`, `expired`, `skipped`, `notified`,
`uploaded`, `transcribed`, `dispatched`, `replied`, `played` (the `audio.memo.*`
journey stages: a voice memo that is uploaded, transcribed, dispatched to an agent,
answered aloud, and played back on the device), `progressed` (`agent.invocation.progressed`:
a running turn said what it is doing, one interim assistant message), `detached` (`agent.invocation.detached`: a running turn outlived its lane's wall clock and gave its slot back instead of being stopped; not terminal), `narrated`
(`audio.memo.narrated`: one such progress line spoken aloud for the car while the memo's
turn still runs), `voiced` (`audio.memo.voiced`: one sentence-chunk of the reply rendered and
uploaded, so the car starts speaking before the whole reply is rendered; a reply short enough for one
file publishes one, chunk 1 of 1, when that file is up), `mirrored`
(`audio.memo.mirrored`: the phone's Earshot copy of the reply was dropped late, after `replied`)), `routed` (`audio.memo.routed`: the fast in-car conversation layer decided one turn; payload names `conv_id` and `action`), `delegated` (`audio.memo.delegated`: a confirmed brief from that layer was dispatched to an agent; payload names `brief_id`, `brief_sha256`, `command_id`).

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
thread:<thread_id>               # agent conversation (conversation.turn.*); not messaging
turn:<turn_id>
invocation:<invocation_id>
session:<session_id>             # agent CLI session (was cli_session)
process:<process_id>
transcription:<transcription_id>
usage:<usage_id>
allowance:<provider>:<account_id>:<window>
file:<sha256(file_path)|file_id>
memo:<memo_id>                   # audio.memo.*: every stage of one voice memo journey
earshot:<event_id>               # audio.earshot.*: one dropped sound file, event_id = uuid5(NAMESPACE_URL, "s3://earshot/" + key)
sync:<run_id>                    # finance: one tiller sync run
account:<account_id>             # finance: per-account transaction/paycheck order
transaction:<txn_id>
subscription:<series_id>         # finance: recurring-series lifecycle incl. zombie strikes
projection:liquid                # finance: single household-wide projection bucket
clock:<clock_system>:<principal> # attendance: one worker/system time-clock state bucket
duty:<duty_id>                   # crank: every fact of one Wingman duty session (duty.*, alert.raised)
project:<project_slug>           # project: one bucket per pjangler project; activity audiences and deployments share it
call:<call_id>                   # telephony: every event of one phone call, recording and transcript included
line:<line>                      # messaging carrier texts (sms/mms/rcs/imessage) on one line ("cell", "imessage" or a Telnyx E.164)
messaging:<platform>:<thread_id> # messaging: one platform-native thread; a threadless notified alert uses its notification_id
invoice:<client>                 # billing: every invoice fact of one client (billing.invoice.*); `invoice:unverified` while the command was refused before its client was proven
```

Pick the narrowest bucket that captures the event's natural ordering.
A `conversation.message.appended` uses `turn:<turn_id>`. A
`cli.stdout.appended` uses `process:<process_id>`. An
`audio.transcription.completed` uses `transcription:<transcription_id>`,
and every `audio.intake.detected` extracted from that transcript shares the
bucket so the intakes order after the completion that caused them;
an `audio.file.received` uses `file:<sha256(file_path)>` so re-detections
of the same artifact form a stable bucket. Every `audio.memo.*` stage uses
`memo:<memo_id>`, where `memo_id = uuid5(NAMESPACE_URL, "s3://holocene-auto/" + key)`. A `project.activity.recorded`
uses `project:<project_slug>` — the slug from the project's `.project.json`
— never `activity:<slug>`; the bucket names the aggregate, not the entity.
A `project.artifact.created` event uses the same `project:<project_slug>`
bucket: the artifact is a fact of the same aggregate.
An `llm.usage.recorded` event uses `usage:<usage_id>`, and an
`llm.allowance.observed` event uses
`allowance:<provider>:<account_id>:<window>` so retries and observations for
one account window form a deterministic sequence.

### 11.2 `idempotency_key` rules

For commands, idempotency is `<action>:<entity-scope>`:

```
agent.invocation.start : target:<target_agent_id>:command:<command_id>
cli.process.spawn      : cli_session:<session_id>:cmd:<sha256(args)>
agent.tool.invoke      : invocation:<invocation_id>:tool_call:<id>
billing.invoice.generate : client:<client>:command:<command_id>
```

A retry of the same command MUST present the same `idempotency_key`. Bloodbank
deduplicates on `(idempotency_key, command_id)` before forwarding.

### 11.3 Portfolio payload lineage

Every `portfolio` payload repeats the envelope lineage as
`data.correlation_id` and `data.causation_id`. The values MUST equal
`correlationid` and `causationid` exactly; publishers and consumers reject a
cross-layer mismatch instead of repairing it.

`bloodbank.portfolio.intake.received` is the only portfolio root contract.
Its envelope `causationid` and payload `data.causation_id` are both null. Every
other portfolio event has a non-null causation UUID identifying its immediate
parent event or command. The correlation UUID remains unchanged for the full
intake-to-terminal chain.

### 11.4 Project activity audience rule

`bloodbank.project.activity.recorded` carries its audience in
`data.audience` (`internal` | `external`), never in the type: §5 makes the
action a past-tense verb and §7 makes the entity a noun, so an adjective
cannot be an address. One type, one schema, one ordering bucket
(`project:<slug>`); consumers that want only client-safe reports filter on
`data.audience == "external"`, exactly as they filter on `actor.provider`.

The schema and `assert_project_invariants` (validate.py) together enforce:

- `ordering_key == "project:" + data.project.slug`
- `correlationid == data.generator.run_id` (one skill run is one correlation;
  the internal and external events of a run share it)
- `data.window`: `end > start`, `duration_seconds == end - start`,
  `basis == cap_24h` ⇒ 86400 s, `basis == previous_report` ⇒
  `previous_event_id` is the prior event of the SAME audience
- `data.tokens`: every `by_agent` bucket sums, and `total` is the sum of buckets
- `audience == external` ⇒ `sources` and `tickets` are absent, and
  `report.title|raw|markdown` contain no `<identifier>-<n>` ticket key,
  no commit sha, no absolute workstation path; `report.html` contains no ticket key
- `audience == internal` ⇒ `sources` and `tickets` are present

Hygiene inherited from `reporting`: no credentials, no stderr dumps, no
absolute filesystem paths anywhere in `data`; `additionalProperties: false`
at every level.

`bloodbank.project.deployment.completed` / `.failed` share only the bucket
rule (`ordering_key == "project:" + data.project.slug`), `data.schema_version == 1`
and the path hygiene above; `assert_project_invariants` skips the window,
token and audience rules for them. `data.summary` is the one line a
notification shows.

`bloodbank.project.artifact.created` also shares only the bucket rule and the
path hygiene, plus artifact-specific invariants enforced by the same
function: every `content.ref` equals `sha256:` + its sibling digest
(`storage: "content-addressed"`, never a path or URL), a `render` twin (when
present) has a different digest from the canonical form, and
`envelope.causationid == data.origin.source_event.id` — the artifact fact is
caused by the agent event whose bytes were captured. `data.origin.basis` is
one of `agent_tool_write`, `conversation_reply_mermaid`,
`conversation_reply_delivered`, `project_activity_report`,
`operator_resolution`; attribution always
comes from the canonical pjangler registry or an operator resolution of a
held capture, never a guess. `project_activity_report` is the one basis whose
project is read from the source event itself (a `project.activity.recorded`
report names its own project, there is no working directory to attribute):
`origin.source_event.type` must be `bloodbank.project.activity.recorded` (and
that source type admits only this basis or `operator_resolution`), the
canonical form is the report markdown and `artifact.render` is the report's
self-contained HTML. Qualification is strict: a write qualifies only
with the full structural shape (path + full inline content, user-facing
media type, non-denied location) AND an explicit user-facing signal — a
producer delivery marker or a project producer-output tree (`_bmad_output/`,
`_bmad-output/`; evidence/logs/scratch/tmp excluded). Genuine binaries
(PNG/PDF) are captured only from explicitly delivered local files read under
operator-allowlisted roots, never from inline strings. The
bytes never travel in the event; they live in the sidecar's content-addressed
store before the event is published (docs/project-artifact-sidecar.md).

---

## 12. Schema directory layout

Bloodbank schemas live under `bloodbank/schemas/` in this repo. The tree is
keyed by **domain**, not by contract version — §3.1 removed the version token
from the type, so there is no `v1/` or `v2/` tier to sort into:

```
bloodbank/schemas/
  _common/
    cloudevent_base.v1.json
    types.v1.json
    portfolio_data.v1.json
  bloodbank/
    <domain>/
      <entity>.<action>.json
```

The `_common/*.v1.json` filenames keep their suffix on purpose: those are
JSON Schema documents with their own revision line (§13's `<n>`), not event
types. Everything under `bloodbank/<domain>/` is named for the fact it
describes and nothing else.

There are 20 live domains — `agent`, `attendance`, `audio`, `billing`,
`cli`, `conversation`, `crank`, `curator`, `finance`, `lifecycle`, `llm`,
`messaging`, `portfolio`, `project`, `repo`, `reporting`, `review`,
`system`, `telephony`, `workflow` — matching the active half of
`ALLOWED_DOMAINS` in `services/agent-hooks/core/validate.py`. A domain
directory appears when its first schema does.

Each schema:

- `$id` MUST be `https://33god.dev/schemas/bloodbank/<domain>/<entity>.<action>.json`.
- MUST `$ref` `../../_common/cloudevent_base.v1.json` — two levels up, not
  three. The `v<N>/` tier that made it three is gone.
- MUST set `properties.type.const` to the full 4-token type string.
- MUST set `properties.kind.const` to `event` or `command`.
- MUST set `properties.domain.const` to match segment 2 of `type`.

There is no provider-named subdirectory (for example,
`bloodbank/schemas/bloodbank/copilot/`) — provider identity does not shape
the schema tree.

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

- `dataschema` — `apicurio://holyfields/bloodbank.<domain>.<entity>.<action>/versions/<n>`
- `schemaref` — `bloodbank.<domain>.<entity>.<action>.v<n>` (string)

The `<n>` in both is the schema revision described in §3.1, and it is the only
version number left in the contract. It is expected to move; the type and the
subject are not.

The Apicurio registry's artifact ID is the 4-token type string, not the
filesystem path. Holyfields' registration script (`scripts/register-schemas.sh`
or equivalent) uses the type as the key when uploading.

---

## 14. Canonical event sequence for an agent turn

For a single agent turn from any CLI (Claude, Copilot, Codex, future), the
normalized event sequence is:

```
bloodbank.conversation.thread.created         # only on first turn of a thread
                                                  # OR
bloodbank.conversation.thread.resumed         # if a known thread is reopened
bloodbank.conversation.turn.started
bloodbank.agent.invocation.started
bloodbank.agent.session.started               # agent CLI session (supersedes cli.session.started)
bloodbank.cli.process.spawned                 # CLI-backed paths only
bloodbank.cli.stdout.appended                 # 0..n; chunked
bloodbank.cli.stderr.appended                 # 0..n; chunked
bloodbank.llm.request.sent                    # protocol boundary
bloodbank.llm.response.received               # protocol boundary
bloodbank.agent.tool.requested            # 0..n
bloodbank.agent.tool.invoked              # 0..n
bloodbank.agent.tool.completed            # 0..n
bloodbank.agent.invocation.progressed     # 0..n; interim assistant messages, between the tool rounds
bloodbank.agent.invocation.detached       # 0..1; the turn ran past its lane's wall clock and runs on (late reply)
bloodbank.conversation.message.appended       # durable transcript record
bloodbank.agent.invocation.completed
bloodbank.conversation.turn.completed
```

Claude, Copilot, and Codex adapters MUST emit the same sequence; only
`actor.*` and payload details differ.

---

## 15. Verifier checks

`ops/smoketest/smoketest-bloodbank-naming.sh` (`mise run
smoketest:bloodbank-naming`) is a stdlib verifier — no Docker — that walks the
§14 sequence for each emitter and asserts:

```
type matches §2 regex
type has exactly 4 dot-separated tokens
tokens[0] == "bloodbank"
tokens[1] in §6 domain allowlist
tokens[2] in §7 entity allowlist
tokens[3] in §8.1 for kind=event, §8.2 for kind=command
tokens ∩ §9 banned tokens == ∅
subject == "bloodbank." + {evt|cmd|rpy} + "." + tokens[1..4].join(".")
subject's kind marker matches envelope.kind
source, actor, subject, correlationid, ordering_key all present on every event
actor.cli ∈ {claude, copilot, codex, ...} per actual emitter
ordering_key is stable across events on the same entity
```

---

## 16. References

- [CloudEvents 1.0 spec](https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md) — `type`, `source`, `subject`, duplicate detection via `source + id`.
- [NATS subject-based messaging](https://docs.nats.io/nats-concepts/subjects) — dot-hierarchy guidance.
- Source directives: `~/d/Inbox/Bloodbank ChatGPT Discussion 2.md` (2026-05-14).
- Repo conventions: `bloodbank/CLAUDE.md`, `bloodbank/compose/nats/streams.json`.

---
