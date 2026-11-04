const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');

const { buildEnvelope, publish, validateEnvelope } = require('../src/nats.ts');
const { sampleEnvelope } = require('../src/bindings.ts');
const { eventSchemas, canonicalSchemaDocuments } = require('../src/nodes/Bloodbank/eventSchemas.ts');
const { candybarRevision } = require('../src/candybar-revisions.ts');
const fixtures = JSON.parse(readFileSync(resolve(__dirname, '../../../ops/fixtures/cron-candybar-contracts.json')));
const CREATED = fixtures.created_v1.type;
const DELETED = fixtures.deleted.type;

test('codegen preserves both closed branches and revision-specific field requirements', () => {
  const schema = eventSchemas.find((s) => s.type === CREATED);
  assert.deepEqual(schema.dataRevisions.map((r) => r.schemaVersion), [1, 2]);
  const [cron, finite] = schema.dataRevisions;
  assert.equal(cron.dataFields.find((f) => f.name === 'schema_version').jsonType, 'integer');
  assert.equal(finite.dataFields.find((f) => f.name === 'schema_version').jsonType, 'integer');
  assert.equal(cron.dataFields.find((f) => f.name === 'schedule').required, true);
  assert.equal(cron.dataFields.some((f) => f.name === 'dates'), false);
  assert.equal(finite.dataFields.some((f) => f.name === 'schedule'), false);
  assert.equal(finite.dataFields.find((f) => f.name === 'dates').required, true);
  assert.equal(finite.dataFields.find((f) => f.name === 'timezone').required, true);
  // A union is not the intersection of its property lists or the union of
  // its required sets: that would drop dates, or incorrectly require schedule.
  assert.equal(schema.dataFields.find((f) => f.name === 'schedule').required, false);
  assert.equal(schema.dataFields.find((f) => f.name === 'dates').required, false);
  const document = canonicalSchemaDocuments.find((s) => s.$id === schema.schemaId);
  assert.equal(document.oneOf.length, 2);
  for (const revision of [1, 2]) {
    const selected = candybarRevision(document, revision);
    assert.equal(selected.properties.data.additionalProperties, false);
    assert.equal(selected.properties.schemaref.const, `${CREATED}.v${revision}`);
  }
  assert.equal(document.oneOf.length, 2, 'selection must not mutate the generated union');
});

test('n8n publisher builds exact handles and identity for v1, v2 and deleted without transport', () => {
  for (const fixture of Object.values(fixtures)) {
    const { envelope, subject } = buildEnvelope({
      type: fixture.type, data: fixture.data, eventId: fixture.id, observedAt: fixture.time,
    });
    assert.equal(subject, fixture.subject);
    assert.equal(envelope.dataschema, fixture.dataschema);
    assert.equal(envelope.schemaref, fixture.schemaref);
    assert.equal(envelope.ordering_key, fixture.ordering_key);
    assert.deepEqual(envelope.data, fixture.data);
  }
});

test('n8n publisher never assumes v1 for unknown or missing registration revisions', () => {
  for (const revision of [undefined, null, 0, 3, '1', true]) {
    assert.throws(() => buildEnvelope({
      type: CREATED, data: { ...fixtures.created_v1.data, schema_version: revision },
    }), /unsupported.*schema_version/);
  }
  assert.throws(() => buildEnvelope({
    type: DELETED, data: { ...fixtures.deleted.data, schema_version: 2 },
  }), /unsupported.*schema_version/);
});

test('v1 and deletion samples select explicit closed data shapes and resolve primitive refs', () => {
  for (const type of [CREATED, DELETED]) {
    const envelope = sampleEnvelope(type, 'event');
    assert.equal(envelope.data.schema_version, 1);
    assert.equal(envelope.ordering_key, `candybar:${envelope.data.candybar_id}`);
    assert.doesNotThrow(() => validateEnvelope(type, envelope));
  }
});

test('AJV traverses revision branches and refs, rejecting mismatched handles and forbidden fields', () => {
  for (const original of Object.values(fixtures)) {
    for (const field of ['dataschema', 'schemaref']) {
      const bad = { ...original, [field]: `${original[field]}-wrong` };
      assert.throws(() => validateEnvelope(original.type, bad), /schema validation failed/);
    }
    const bad = structuredClone(original);
    bad.data.candybar_id = 'not-a-uuid';
    assert.throws(() => validateEnvelope(original.type, bad), /schema validation failed/);
  }
  const mixed = structuredClone(fixtures.created_v2);
  mixed.data.schedule = '* * * * *';
  assert.throws(() => validateEnvelope(CREATED, mixed), /schema validation failed/);
});

test('finite v2 explicitly fails unsupported n8n semantic validation, not relabelled as v1', async () => {
  assert.throws(() => validateEnvelope(CREATED, fixtures.created_v2), /finite candybar v2.*unsupported/);
  let connections = 0;
  await assert.rejects(
    publish({ type: CREATED, data: fixtures.created_v2.data, validate: false },
      async () => { connections += 1; throw new Error('must not connect'); }),
    /finite candybar v2.*unsupported/,
  );
  assert.equal(connections, 0);
});

test('candybar publication checks ordering and closed data before opening any connection', async () => {
  let connections = 0;
  const noConnect = async () => { connections += 1; throw new Error('must not connect'); };
  for (const fixture of [fixtures.created_v1, fixtures.deleted]) {
    await assert.rejects(
      publish({ type: fixture.type, data: fixture.data, orderingKey: 'candybar:wrong' }, noConnect),
      /ordering_key/,
    );
    const data = { ...fixture.data, extra: true };
    await assert.rejects(publish({ type: fixture.type, data }, noConnect), /schema validation failed/);
  }
  const data = { ...fixtures.deleted.data };
  delete data.last_invocation_id;
  await assert.rejects(publish({ type: DELETED, data }, noConnect), /schema validation failed/);
  assert.equal(connections, 0);
});
