# Standalone Lifecycle Contracts and Transport

**Authority:** `delorenj/lifecycle`

**Contract and transport owner:** Bloodbank

**CloudEvent type namespace:** v1

**Current snapshot schema artifact:** v3

**Current obligation evidence schema artifact:** v2

Bloodbank owns versioned schemas, naming, validation, NATS subjects, Dapr
transport, and producer authorization. It does not evaluate transitions,
derive legal state, persist operational lifecycle state, or publish authority
events on another service's behalf. The standalone `delorenj/lifecycle`
component is the only operational lifecycle writer.

The older `mission`, `roadmap`, `checkpoint`, and `gate` schemas remain wire
compatibility contracts. Their presence does not authorize a Bloodbank or n8n
writer. Any future authority publication using them must originate from the
standalone component.

## Canonical authority contracts

| Contract | Purpose |
| --- | --- |
| `lifecycle.observation.recorded` event | Lossless source identity, provenance, immutable source time, payload hash, and source payload. |
| `lifecycle.obligation_evidence.submitted` event | Completed skill evidence for one exact authority-owned obligation occurrence, target actor, and invocation. It is an authority input, never a satisfaction verdict. |
| `lifecycle.snapshot.updated` event | Versioned authoritative state plus legal frontier, obligation occurrences, blockers, gates, versioned capabilities, provenance, freshness, and outbox identity. |
| `lifecycle.status.updated` event | Versioned status/health transition with prior state, provenance, freshness, and outbox identity. |
| `lifecycle.blocker.detected` / `.resolved` events | Versioned blocker activation and resolution. |
| `lifecycle.intent.submit` command | Actor- and capability-scoped intent with idempotency and optimistic concurrency. |
| `lifecycle.intent.submit` reply | Stable, non-ambiguous authority verdict. |

Every authoritative event carries `spec_version`, `state_version`, authority
`provenance`, observation `freshness`, and transactional-outbox `publication`
metadata. The CloudEvent `id`, outbox ID, aggregate ID/version, and event
sequence give consumers enough stable identity to deduplicate and rebuild
projections.

Every obligation also carries a strict `skill_ref` with exactly two fields:
canonical Skillex `name` (lowercase kebab form) and a non-empty `selector` for
the skill version, tag, or revision. Lifecycle supplies that reference and
Momo/Skillex interprets it as invocation intent. Bloodbank only defines the
wire shape; commands, models, providers, and execution policy are deliberately
not part of `skill_ref`.

Snapshot schema v3 is a deliberate incompatible artifact revision under the
unchanged `bloodbank.v1.lifecycle.snapshot.updated` CloudEvent type. Its
`contract_version`, `dataschema`, and `schemaref` are all 3. It retains v2's
required authority-owned `capability_version` and additionally requires every
obligation to carry an RFC 4122 `obligation_instance_id` plus immutable
`activated_at`. The instance identifies one pending occurrence of a reusable
rule: it remains stable for that occurrence and changes if the rule becomes
pending again in a later lifecycle cycle. Snapshot v1 and v2 remain valid for
existing consumers but are not the current producer artifact. Current clients
derive capability and obligation-occurrence identity from v3 rather than
guessing either value.

`lifecycle.obligation_evidence.submitted` v2 requires the exact lifecycle,
repository, obligation rule identity and kind, active
`obligation_instance_id`, authority-selected target actor, canonical skill
reference, stable invocation ID, completion time, and an integrity-addressed
completion artifact. Its evidence kind is
`skill_completion` and outcome is `completed`; invocation, request, or review-
requested records do not validate as completion. Lifecycle alone correlates
and evaluates this input against the exact active occurrence and activation
time, records the resulting observation, and determines whether an obligation
becomes satisfied or state may advance. Evidence for a prior occurrence, or
evidence observed before the active occurrence was activated, cannot satisfy
it. Evidence v1 remains a compatibility artifact but cannot identify an
occurrence and is not accepted by the current authority consumer.

`status.updated` preserves the extracted reconciler's real first-publication
shape without weakening repository identity: `state_version=1` requires
`previous_state_version=null` and `previous=null`, while `repo` remains a
required non-empty slug. For every later state version, both prior fields are
required and non-null. The standalone repository/publisher boundary must
enrich `repo`; an empty placeholder never validates on the wire.

## Same-type command and reply resolution

The command and reply intentionally share the canonical CloudEvent type:

```text
bloodbank.v1.lifecycle.intent.submit
```

Their subjects and schema artifacts are distinct:

```text
bloodbank.cmd.v1.lifecycle.intent.submit  -> intent.submit.command.v1.json
bloodbank.rpy.v1.lifecycle.intent.submit  -> intent.submit.reply.v1.json
```

Runtime schema selection is an explicit `(type, kind)` registry. Type-only
lookup for this pair fails, unregistered kinds fail, subject/type/kind
mismatches fail before JSON Schema validation, and cross-kind payloads fail
against the selected schema. Existing unambiguous event consumers retain
type-only conventional lookup.

Commands carry top-level actor, stable `command_id`, stable
`idempotency_key`, `delivery=single_consumer`, data capability context, and
`expected_state_version`. Replies use these verdicts:

| Verdict | New mutation? | Meaning |
| --- | --- | --- |
| `accepted` | no | Valid command accepted for later application. |
| `applied` | yes | Authority applied it and identifies the resulting version/event. |
| `idempotent` | no | The same effect was already applied; the prior version/event is returned. |
| `stale` | no | `expected_state_version` did not match. |
| `unauthorized` | no | Actor/capability check failed. |
| `malformed` | no | Command data could not be interpreted. |
| `illegal` | no | Intent is not in the legal frontier. |

The reply schema makes `mutated=true` impossible for accepted, idempotent, or
rejected verdicts. Rejection and accepted replies also require null resulting
version/event fields, so they cannot imply a state change.

## Real observation source: `repo.task.*`

The smallest existing live producer seam is the n8n Bloodbank node and the
canonical repository task family:

```text
bloodbank.v1.repo.task.created
bloodbank.v1.repo.task.recorded
bloodbank.v1.repo.task.completed
```

The node's explicit producer policy includes these three types and excludes
the entire lifecycle domain. Schema existence alone is not producer
authorization. For a task event, the node emits:

- subject `bloodbank.evt.v1.repo.task.<action>`;
- a required, non-blank `repo` and `task_id` validated against the selected
  canonical repo-task schema before any NATS connection is opened;
- producer-supplied CloudEvent ID and source observation `time` when provided;
  otherwise `time` comes only from the contract's canonical payload timestamp
  (`updated_at` or `completed_at`), and events with neither source fail closed;
- a deterministic default RFC 4122 UUID derived from event type, repo, task ID,
  immutable source time, and the SHA-256 fingerprint of canonicalized payload
  JSON, so identical retries deduplicate while materially different updates do
  not;
- `dataschema`, `schemaref`, actor, correlation, causation, and a deterministic
  `task:<repo>:<task_id>` ordering key; default correlation is scoped by both
  repo and task ID;
- the original repo-task payload, with no lifecycle verdict or state derived
  by Bloodbank.

The same generated schema metadata enforces required fields, JSON field types,
enums, string bounds/patterns, and RFC 3339 date-time formats for all three
authorized `repo.task.*` sources in the live publish path. This is intentionally
not a claim that every other n8n-authorized event has full field-level runtime
schema validation.

`BLOODBANK_EVENTS` persists `bloodbank.evt.v1.>` with limits retention. The
standalone lifecycle observation consumer uses the narrower transport seam:

```text
bloodbank.evt.v1.repo.task.>
```

It may ingest the source event directly. If an adapter publishes
`lifecycle.observation.recorded`, it must losslessly preserve the source event
ID, type, subject, source, producer, ordering key, immutable source time,
payload, and payload hash. Adapter receipt time is not observation time.
Lifecycle alone decides what the observation means.

The executable proof is:

```bash
cd integrations/n8n-nodes-bloodbank
npm test
```

It builds retry-stable repo-task envelopes, checks cross-repository correlation
separation, proves invalid payloads never open the fake NATS transport, validates
the wire envelope through Bloodbank's Python validator, checks exact subject/body
identity, and verifies that the stream and lifecycle subscription seam cover the
subject.

## Validation

### Schema inventory at closure

The extraction-source pin
`03415705a39d77f1e6d73c8a9c92ee177320df7e` contained 72 JSON schemas: 2
common definitions and 70 wire contracts (64 event, 6 command, 0 reply).
This closure contains 85 JSON schemas: 5 common definitions and 80 wire
contracts (70 event, 7 command, 1 reply).

| Domain | Source pin | Contract closure |
| --- | ---: | ---: |
| `_common` | 2 | 5 |
| `agent` | 9 | 9 |
| `attendance` | 7 | 7 |
| `audio` | 5 | 5 |
| `cli` | 6 | 6 |
| `conversation` | 5 | 5 |
| `curator` | 4 | 4 |
| `finance` | 12 | 12 |
| `lifecycle` | 6 | 14 |
| `llm` | 2 | 2 |
| `repo` | 9 | 9 |
| `reporting` | 3 | 3 |
| `system` | 2 | 2 |

Before mutation, `mise run smoketest:schemas` passed 72 schema files, 70
contract registrations, 69 naming checks, 15 maintenance/reporting tests,
and 25 agent-hook bindings. The extracted controller's compatibility baseline
also passed 21 tests and Ruff with its development extras installed.

Run the full offline contract surface:

```bash
mise run smoketest:schemas
```

The focused lifecycle suite covers same-type command/reply selection,
cross-kind rejection, subject binding, all verdict branches, initial and later
status publications, observation time/provenance, blocker presence, snapshot
v1/v2 compatibility, exact snapshot v3 selection, required capability version,
authority-owned obligation occurrence identity, strict completion-evidence v2,
frontier/obligations/gates/capabilities, and an unrelated legacy consumer.

Extraction history and the removal of the embedded writer are recorded in
`docs/lifecycle-controller-extraction-provenance.md`.
