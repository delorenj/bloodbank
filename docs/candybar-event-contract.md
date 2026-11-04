# CAND-51 consumer and mirror handoff

Authority: `docs/event-naming.md` §3.1 and §13.1; governance `t_ff84ad7e`,
PM comment 72. This is schema/validator work only, not Candybar application,
owner-policy, publication, registry upload, deployment, or merge authorization.

## Lookup and revision handling

Canonical files (relative to this repository):

- `schemas/bloodbank/cron/candybar.created.json`: one dispatching schema;
  preserved closed cron v1 and closed finite-only v2, with matching envelope
  `dataschema`, `schemaref` and payload `schema_version`.
- `schemas/bloodbank/cron/candybar.deleted.json`: closed deletion v1.
- `schemas/bloodbank/cron/candybar.updated.json`: unchanged v1 bytes.

`core.validate.load_event_schema(type)` and legacy `load_schema_for(type)`
return the canonical **full union**, not an inferred v1. Exact created
selection is `load_event_schema(type, schema_version=1|2)`;
`load_event_data_schema(type, schema_version=1|2)` is the flat-data helper.
Exact selection is intentionally scoped to this registration fact. Flat-data
lookup without a revision fails explicitly for created, rather than returning
an empty object. Schema paths/refs are resolved locally; no Apicurio or HTTPS
retrieval occurs. `assert_contract` and `validate_envelope` both enforce the
finite calendar/zone/duplicate semantics and created/deleted ordering identity.

Consumers must preserve the revision union or explicitly reject unsupported
revisions. They must not require `schedule` for finite v2, manufacture a cron
string, discard `dates`, or relabel v2 payloads with v1 handles. Shape validation
alone does not establish real calendar dates, equivalent-date uniqueness, IANA
zone existence, or cross-field ordering identity. Use the canonical semantic
validator as well, or fail closed. No gap/fold/past/missed/control policy is
selected by these helpers; those remain owner gate `t_7204a764`.

For downstream finite edits, replace the variant atomically and omit the
obsolete field. Cron: keep `schedule`, omit `dates`. Finite: keep `dates` and
`timezone`, omit `schedule`. `candybar.updated.json` is NOT revised here.

## Audited in-repository consumers

| Consumer | Handling / boundary |
| --- | --- |
| `scripts/validate_schemas.sh` | Recursive object/list `$ref` walk already covers root `allOf` and both `oneOf` branches, checking IDs/fragments without network retrieval. |
| `services/agent-hooks/core/validate.py` | Default lookup retains both branches. Exact created revision helper is explicit. Both runtime entrypoints enforce CAND-51 semantics. |
| `ops/smoketest/smoketest-schema-contract-consistency.sh` | Uses canonical fixture envelopes for created/deleted, not an empty data stub. |
| `ops/smoketest/smoketest-cron-candybar-contracts.py` | Fixture-driven positive/negative runtime and raw JSON Schema branch/ref probes, exact old-data hash, unchanged updated hash, and offline proof. Included in `smoketest:schemas` before the existing Plane binding gate. |
| `services/agent-hooks/health/hook_healthcheck.py` and `ops/smoketest/smoketest-agent-hooks-ssot.sh` | Only flat-data lookup changed to the fail-closed helper. No hook mappings, live health execution, or runtime behavior changed. If retargeted at multi-revision created, they now require explicit revision support rather than silently sampling `{}`. |
| `integrations/n8n-nodes-bloodbank/codegen/generate-events.mjs` | Retains raw canonical documents, emits exact `dataRevisions` including handles/required fields, and a flat union field list whose required flags are the common intersection. Refuses unknown union layouts. Raw schema types/branches/refs are never flattened away. |
| n8n `src/bindings.ts` | Sample generation explicitly selects cron v1; follows primitive refs/nullable branches. This is sample selection, not publisher revision inference. |
| n8n `src/nats.ts`, `src/candybar-revisions.ts` | Builds exact v1/v2/deleted handles from canonical documents and derives `candybar:<id>`. Unknown revisions fail. The typed `publish` path always validates created/deleted before connection. **Finite-v2 publication/semantic validation is explicitly unsupported and fails before transport**, even with `validate: false`, until this consumer implements canonical calendar/IANA semantics. It never silently sends v2 as v1. v1 and deletion remain supported. |
| `services/hermes-gateway/tests/test_contract.py` | Flat-data accesses target other fixed invocation/conversation schemas; no created/deleted consumer and no change required. |
| `scripts/candybar_codegen_schema.py` and `skills/bloodbank-sdk-generation/SKILL.md` | CAND-51-only self-contained codegen view expands the known base/primitive refs, preserves the full envelope union and handle coupling, and refuses unknown layouts/refs. No Holyfields legacy generator was ported or cleaned up. Generated shape types do not replace semantic validation. |

### Typed SDK build

Use the generator-only view below for these two facts, not the generic raw
schema recipe: direct modular `datamodel-codegen` 0.54.0 output produced an
undefined-base import, and root `allOf` beside `oneOf` silently lost branches.
The canonical schema now puts base inheritance inside each closed branch;
the local codegen view additionally expands those known refs for generators
that cannot safely handle modular inheritance. Runtime lookup still returns
the canonical dispatching schema, not this codegen view.

```sh
mkdir -p <ignored-sdk-output>
python3 scripts/candybar_codegen_schema.py created > <ignored-sdk-output>/created.codegen.json
python3 scripts/candybar_codegen_schema.py deleted > <ignored-sdk-output>/deleted.codegen.json
datamodel-codegen --input <ignored-sdk-output>/created.codegen.json \
  --input-file-type jsonschema --output-model-type pydantic_v2.BaseModel \
  --target-python-version 3.12 --enum-field-as-literal one \
  --disable-timestamp --output <ignored-sdk-output>/created_models.py
datamodel-codegen --input <ignored-sdk-output>/deleted.codegen.json \
  --input-file-type jsonschema --output-model-type pydantic_v2.BaseModel \
  --target-python-version 3.12 --enum-field-as-literal one \
  --disable-timestamp --output <ignored-sdk-output>/deleted_models.py
```

TypeScript generators can consume the same self-contained JSON files.
Import/compile the outputs AND exercise fixtures: created v1 requires
`schedule`, v2 requires `dates` and `timezone` with no `schedule`, mismatched
handles fail, and deletion requires explicit nullable `last_invocation_id`.
Do not use coercing Pydantic defaults as wire validation; generated models
are type bindings, while canonical JSON Schema plus `assert_contract` owns
actual wire acceptance. No new model package is committed by this task.

## Post-canonical-landing mirror procedure (carrier only)

Do not copy from an unreviewed worktree or planning commit. After independent
spec/quality review and canonical landing, pin the **landed Bloodbank commit**.
The downstream mirror root below must be its approved canonical-schema root,
with the **same layout** as Bloodbank `schemas/`. These are exact relative
copy paths, not alternate `.v2.json` names:

```text
Bloodbank source                              Downstream mirror-root target
schemas/bloodbank/cron/candybar.created.json -> bloodbank/cron/candybar.created.json
schemas/bloodbank/cron/candybar.deleted.json -> bloodbank/cron/candybar.deleted.json
schemas/_common/cloudevent_base.v1.json     -> _common/cloudevent_base.v1.json
schemas/_common/types.v1.json               -> _common/types.v1.json
```

Common files are unchanged dependencies, not an authorization to overwrite
unrelated downstream work. Compare before copying; stop on divergent common
schemas. Holyfields' transitional mirror root is its `schemas/` directory;
other consumers must explicitly nominate their own mirror root. Do not blindly
place these files in a flat `schemas/cron/`: the canonical `../../_common/`
refs require the `bloodbank/cron/` depth. A legacy flat-layout consumer needs a
reviewed lookup-path change, not hand-rewritten schema refs or a guessed copy.

Example commands for the carrier in a clean, reviewed source checkout:

```sh
# Set to the reviewed landed commit and approved downstream schema root.
LANDED_COMMIT=<landed-bloodbank-commit>
MIRROR_ROOT=<approved-downstream-schema-root>
test "$(git rev-parse HEAD)" = "$LANDED_COMMIT"
sha256sum schemas/bloodbank/cron/candybar.{created,deleted,updated}.json \
  schemas/_common/{cloudevent_base,types}.v1.json
# Preserve the paths above. Copy created/deleted only; common dependencies
# must already be byte-identical or be separately reviewed before copying.
mkdir -p "$MIRROR_ROOT/bloodbank/cron"
cp schemas/bloodbank/cron/candybar.created.json "$MIRROR_ROOT/bloodbank/cron/"
cp schemas/bloodbank/cron/candybar.deleted.json "$MIRROR_ROOT/bloodbank/cron/"
cmp schemas/bloodbank/cron/candybar.created.json "$MIRROR_ROOT/bloodbank/cron/candybar.created.json"
cmp schemas/bloodbank/cron/candybar.deleted.json "$MIRROR_ROOT/bloodbank/cron/candybar.deleted.json"
cmp schemas/_common/cloudevent_base.v1.json "$MIRROR_ROOT/_common/cloudevent_base.v1.json"
cmp schemas/_common/types.v1.json "$MIRROR_ROOT/_common/types.v1.json"
BLOODBANK_SCHEMAS_DIR="$MIRROR_ROOT" bash scripts/validate_schemas.sh
```

Record landed commit plus source/target SHA256s, rerun downstream fixtures and
codegen/build, and verify the updated v1 file remains byte-identical (baseline
SHA256 `2fb2432a1fcd8d07ea2774e3cb77b36786d0b6e0d8a5d94f2d2943302aa7c6cd`).
If a consumer cannot preserve the union or enforce finite semantics, explicitly
reject revision 2 pending its own reviewed implementation. This procedure is
not publication permission. Producer deletion outbox/transaction obligations
are specified in §13.1 and are not implemented in Bloodbank.
