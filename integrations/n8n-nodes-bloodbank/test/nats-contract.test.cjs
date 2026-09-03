const assert = require('node:assert/strict');
const test = require('node:test');

const {
  buildEnvelope,
  deterministicUuid,
  publish,
  subjectFor,
  validateEnvelope,
} = require('../src/index.ts');

const COMMAND_TYPE = 'bloodbank.agent.invocation.start';
const COMMAND_ID = '550e8400-e29b-41d4-a716-446655440000';
const EVENT_ID = '6ba7b810-9dad-11d1-80b4-00c04fd430c8';

function commandOptions(overrides = {}) {
  return {
    type: COMMAND_TYPE,
    kind: 'command',
    data: {
      target_agent_id: 'bloodbank-pm',
      prompt: 'Review the current Bloodbank queue.',
      thread_id: 'thread-1',
      turn_id: 'turn-1',
      context: { priority: 'normal' },
    },
    eventId: EVENT_ID,
    commandId: COMMAND_ID,
    observedAt: '2026-09-02T12:34:56.000Z',
    ...overrides,
  };
}

test('publisher builds a complete canonical event envelope', () => {
  const eventId = deterministicUuid('plane.ticket.created:ticket-1');
  const { subject, envelope } = buildEnvelope({
    type: 'bloodbank.repo.task.created',
    kind: 'event',
    data: { repo: '33GOD', task_id: 'ticket-1', title: 'Test' },
    eventId,
    observedAt: '2026-08-26T12:34:56.000Z',
    extensions: {
      workspace: '33god',
      board_id: 'board-1',
      slug: '33GOD',
      provider_event_type: 'plane.ticket.created',
    },
  });
  assert.equal(subject, 'bloodbank.evt.repo.task.created');
  assert.equal(envelope.id, eventId);
  assert.equal(envelope.kind, 'event');
  assert.equal(envelope.domain, 'repo');
  assert.deepEqual(envelope.actor, {
    type: 'service',
    agent_id: 'bloodbank.integration.n8n',
  });
  assert.equal(envelope.ordering_key, 'repo:ticket-1');
  assert.equal(envelope.provider_event_type, 'plane.ticket.created');
  assert.equal(envelope.data.title, 'Test');
});

test('publisher builds a complete schema-valid invocation command envelope', () => {
  const first = buildEnvelope(commandOptions());
  const retry = buildEnvelope(commandOptions());

  assert.equal(first.subject, 'bloodbank.cmd.agent.invocation.start');
  assert.equal(first.envelope.subject, first.subject);
  assert.equal(first.envelope.kind, 'command');
  assert.equal(first.envelope.delivery, 'single_consumer');
  assert.equal(first.envelope.id, EVENT_ID);
  assert.equal(first.envelope.command_id, COMMAND_ID);
  assert.equal(first.envelope.correlationid, COMMAND_ID);
  assert.equal(first.envelope.causationid, null);
  assert.equal(
    first.envelope.idempotency_key,
    `agent.invocation.start:target:bloodbank-pm:command:${COMMAND_ID}`,
  );
  assert.equal(first.envelope.idempotency_key, retry.envelope.idempotency_key);
  assert.equal(
    first.envelope.dataschema,
    'apicurio://holyfields/bloodbank.agent.invocation.start/versions/1',
  );
  assert.equal(first.envelope.schemaref, 'bloodbank.agent.invocation.start.v1');
  assert.equal('ordering_key' in first.envelope, false);
  assert.doesNotThrow(() => validateEnvelope(COMMAND_TYPE, first.envelope));

  const otherTarget = buildEnvelope(commandOptions({
    data: { target_agent_id: 'research', prompt: 'Review the queue.' },
  }));
  assert.notEqual(otherTarget.envelope.idempotency_key, first.envelope.idempotency_key);
  assert.match(otherTarget.envelope.idempotency_key, /target:research:/);
});

const invalidCommandCases = [
  ['wrong type', (envelope) => { envelope.type = 'bloodbank.agent.invocation.started'; }],
  ['wrong kind', (envelope) => { envelope.kind = 'event'; }],
  ['wrong subject', (envelope) => { envelope.subject = 'bloodbank.evt.agent.invocation.start'; }],
  ['wrong delivery', (envelope) => { envelope.delivery = 'broadcast'; }],
  ['wrong dataschema', (envelope) => { envelope.dataschema = 'apicurio://wrong/schema'; }],
  ['wrong schemaref', (envelope) => { envelope.schemaref = 'bloodbank.agent.invocation.start.v2'; }],
  ['blank idempotency key', (envelope) => { envelope.idempotency_key = '   '; }],
  ['invalid envelope UUID', (envelope) => { envelope.id = 'not-a-uuid'; }],
  ['invalid command UUID', (envelope) => { envelope.command_id = 'not-a-uuid'; }],
  ['invalid correlation UUID', (envelope) => { envelope.correlationid = 'not-a-uuid'; }],
  ['invalid causation UUID', (envelope) => { envelope.causationid = 'not-a-uuid'; }],
  ['invalid calendar date', (envelope) => { envelope.time = '2026-02-30T12:00:00Z'; }],
  ['missing target', (envelope) => { delete envelope.data.target_agent_id; }],
  ['missing prompt', (envelope) => { delete envelope.data.prompt; }],
  ['blank target', (envelope) => { envelope.data.target_agent_id = '   '; }],
  ['blank prompt', (envelope) => { envelope.data.prompt = '   '; }],
  ['blank thread ID', (envelope) => { envelope.data.thread_id = '   '; }],
  ['non-object context', (envelope) => { envelope.data.context = ['not', 'an', 'object']; }],
  ['malformed actor', (envelope) => { envelope.actor = { type: 'service' }; }],
  ['missing envelope ID', (envelope) => { delete envelope.id; }],
  ['missing command ID', (envelope) => { delete envelope.command_id; }],
  ['missing correlation ID', (envelope) => { delete envelope.correlationid; }],
  ['missing causation ID', (envelope) => { delete envelope.causationid; }],
];

for (const [label, mutate] of invalidCommandCases) {
  test(`full command schema rejects ${label}`, () => {
    const { envelope } = buildEnvelope(commandOptions());
    mutate(envelope);
    assert.throws(
      () => validateEnvelope(COMMAND_TYPE, envelope),
      /schema validation failed/,
    );
  });
}

test('invalid command input fails before a NATS connection or publish', async () => {
  assert.equal(publish.length, 2, 'publish exposes its bounded connector seam');
  let connectCalls = 0;
  const connectNats = async () => {
    connectCalls += 1;
    throw new Error('NATS must not be reached');
  };

  await assert.rejects(
    publish(commandOptions({ observedAt: '2026-02-30T12:00:00Z' }), connectNats),
    /schema validation failed/,
  );
  await assert.rejects(
    publish(commandOptions({ kind: 'event' }), connectNats),
    /schema validation failed/,
  );
  await assert.rejects(
    publish(commandOptions({
      data: {
        target_agent_id: 'bloodbank-pm',
        prompt: 'Review the queue.',
        context: 'not-an-object',
      },
    }), connectNats),
    /schema validation failed/,
  );
  assert.equal(connectCalls, 0);
});

test('subject generation distinguishes events, commands, and replies', () => {
  const type = 'bloodbank.agent.invocation.start';
  assert.equal(subjectFor(type, 'event'), 'bloodbank.evt.agent.invocation.start');
  assert.equal(subjectFor(type, 'command'), 'bloodbank.cmd.agent.invocation.start');
  assert.equal(subjectFor(type, 'reply'), 'bloodbank.rpy.agent.invocation.start');
});

test('extensions cannot replace canonical envelope fields', () => {
  assert.throws(
    () => buildEnvelope({
      type: 'bloodbank.repo.task.created',
      data: { repo: '33GOD', title: 'Test' },
      extensions: { type: 'replacement' },
    }),
    /cannot replace a canonical envelope field/,
  );

  for (const key of ['command_id', 'idempotency_key', 'delivery', 'data']) {
    assert.throws(
      () => buildEnvelope(commandOptions({ extensions: { [key]: 'replacement' } })),
      /cannot replace a canonical envelope field/,
    );
  }
});
