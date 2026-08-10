# Authoring a Bloodbank event schema

Bloodbank `schemas/` owns the wire-level shape of every bloodbank event. Treat it like an API contract: a breaking change is a `.v2` file, never an edit-in-place.

## The two-layer schema model

Every event schema is the **base envelope** + a **per-event extension**:

```
bloodbank/schemas/
├── _common/
│   ├── cloudevent_base.v1.json    # CloudEvents 1.0 + 33GOD extension fields
│   └── types.v1.json              # shared $defs (uuid, timestamp, …)
└── bloodbank/
    └── v1/
        └── <domain>/
            └── <entity>.<action>.v<N>.json # YOUR schema, extends the base
```

Per-event schemas use `allOf` to inherit the base, then lock the `type` / `domain` consts and define the `data` object:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://33god.dev/schemas/bloodbank/v1/agent/session.started.v1.json",
  "title": "Agent Session Started Event",
  "type": "object",
  "allOf": [ { "$ref": "../../../_common/cloudevent_base.v1.json" } ],
  "properties": {
    "type":   { "const": "bloodbank.v1.agent.session.started" },
    "kind":   { "const": "event" },
    "domain": { "const": "agent" },
    "data": {
      "type": "object",
      "properties": {
        "session_id":        { "$ref": "../_common/types.v1.json#/$defs/uuid" },
        "working_directory": { "type": "string", "minLength": 1 },
        "git_branch":        { "type": "string" },
        "started_at":        { "$ref": "../_common/types.v1.json#/$defs/timestamp" }
      },
      "required": ["session_id", "working_directory", "started_at"],
      "additionalProperties": false
    }
  },
  "required": ["type", "kind", "domain", "data"]
}
```

Key rules:

- `$id` follows `https://33god.dev/schemas/bloodbank/v1/<domain>/<entity>.<action>.v<N>.json` — the URL is logical, not fetched at runtime.
- `type` is **const-locked** to `bloodbank.v1.<domain>.<entity>.<action>`.
- `kind` is **const-locked** to `event`, `command`, or `reply`; event subjects derive as `bloodbank.evt.v1.<domain>.<entity>.<action>`.
- `domain` is **const-locked** to the top-level folder name.
- `data` is the only payload field producers populate; everything else is envelope-level.
- Use `$ref` into `types.v1.json` for shared primitives (uuid, timestamp). Don't redeclare them inline.

## Workflow

From the Bloodbank repo checkout (`~/code/33GOD/bloodbank`):

```bash
mise run validate:schemas   # JSON Schema + 33GOD-specific structural rules
mise run smoketest:schema-contract-consistency
mise run smoketest:schemas  # schema tree + naming contract + agent-hooks SSOT
```

Schema files are committed directly. The smoke tests fail if a schema's `$id`, `type`, `kind`, or contract-facing fields drift away from the v1 naming rules.

## Building matching envelopes

The canonical hook path builds envelopes through `services/agent-hooks/core/envelope.py`; service producers should either reuse that builder or keep the same field math:

```python
ce_type = "bloodbank.v1.agent.session.started"
envelope = {
    "type": ce_type,
    "subject": "bloodbank.evt.v1.agent.session.started",
    "kind": "event",
    "domain": "agent",
    "schemaref": f"{ce_type}.v1",
    "dataschema": f"apicurio://holyfields/{ce_type}/versions/1",
    "data": {"session_id": session_id, "working_directory": cwd},
}
```

Do not hand-type the subject at every call site. Derive it from `(type, kind)` using the same `bloodbank.<evt|cmd|rpy>.v1...` rule.

## Versioning

- **Additive change** (new optional field, new enum variant): bump the `description`, keep the same `.v<N>.json`. Validate that consumers tolerate the new field.
- **Breaking change** (rename, remove, retype, add required field): copy to `.v<N+1>.json`, keep the `type` form as `bloodbank.v1.<domain>.<entity>.<action>` unless the semantic event name changes, update `dataschema` URIs. Run schema versions in parallel until consumers cut over.

## When to add a `_common` type

Lift a `$defs` entry into `_common/types.v1.json` only when it is reused across ≥ 2 schemas. Premature sharing of "common" types fights schema evolution.

## Anti-patterns

- Treating downstream generated bindings as the source of truth; the JSON Schemas own the wire contract.
- Skipping `mise run smoketest:schemas` before pushing.
- Editing the `cloudevent_base.v1.json` extension fields to fit a one-off use case — propose an ADR in `~/code/33GOD/docs/architecture/` instead.
- Using free-form `data: { type: object }` with no required fields ("anyone can put anything"). Tighten the schema before merging.
