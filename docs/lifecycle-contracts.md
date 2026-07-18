# Standalone Lifecycle Contracts and Transport

**Authority:** `delorenj/lifecycle`

**Contract and transport owner:** Bloodbank

**Contract version:** v1

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
| `lifecycle.snapshot.updated` event | Versioned authoritative state plus legal frontier, obligations, blockers, gates, capabilities, provenance, freshness, and outbox identity. |
| `lifecycle.status.updated` event | Versioned status/health transition with prior state, provenance, freshness, and outbox identity. |
| `lifecycle.blocker.detected` / `.resolved` events | Versioned blocker activation and resolution. |
| `lifecycle.intent.submit` command | Actor- and capability-scoped intent with idempotency and optimistic concurrency. |
| `lifecycle.intent.submit` reply | Stable, non-ambiguous authority verdict. |

Every authoritative event carries `spec_version`, `state_version`, authority
`provenance`, observation `freshness`, and transactional-outbox `publication`
metadata. The CloudEvent `id`, outbox ID, aggregate ID/version, and event
sequence give consumers enough stable identity to deduplicate and rebuild
projections.

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
- stable producer-supplied CloudEvent ID and source observation `time` when
  provided (generated once/current time only when omitted);
- `dataschema`, `schemaref`, actor, correlation, causation, and a deterministic
  `task:<repo>:<task_id>` ordering key;
- the original repo-task payload, with no lifecycle verdict or state derived
  by Bloodbank.

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

It builds a deterministic `repo.task.recorded` envelope, validates it through
Bloodbank's Python validator, publishes it over the raw NATS protocol to a
fake server, checks exact subject/body identity, and verifies that the stream
and lifecycle subscription seam cover the subject.

## Validation

### Schema inventory at closure

The extraction-source pin
`03415705a39d77f1e6d73c8a9c92ee177320df7e` contained 72 JSON schemas: 2
common definitions and 70 wire contracts (64 event, 6 command, 0 reply).
This closure contains 79 JSON schemas: 3 common definitions and 76 wire
contracts (68 event, 7 command, 1 reply).

| Domain | Source pin | Contract closure |
| --- | ---: | ---: |
| `_common` | 2 | 3 |
| `agent` | 9 | 9 |
| `attendance` | 7 | 7 |
| `audio` | 5 | 5 |
| `cli` | 6 | 6 |
| `conversation` | 5 | 5 |
| `curator` | 4 | 4 |
| `finance` | 12 | 12 |
| `lifecycle` | 6 | 12 |
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
frontier/obligations/gates/capabilities, and an unrelated legacy consumer.

Extraction history and the removal of the embedded writer are recorded in
`docs/lifecycle-controller-extraction-provenance.md`.
