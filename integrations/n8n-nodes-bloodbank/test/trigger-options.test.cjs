const assert = require('node:assert/strict');
const test = require('node:test');

const {
  Bloodbank,
  BloodbankTrigger,
  Fleet,
  bindingMatches,
  eventSchemas,
  findLatestMatching,
  manualTestEnvelope,
  matchesDataConditions,
  parseDataConditions,
  sampleEnvelope,
  triggerAccepts,
  validateEnvelope,
  valueAtPath,
} = require('../src/index.ts');

const OPTION_SHAPE = /^[A-Z][\w ]* · [\w ]+ \([a-z_.]+\)$/;

function property(node, name) {
  return node.description.properties.find((candidate) => candidate.name === name);
}

// ---------------------------------------------------------------------------
// One option shape everywhere
// ---------------------------------------------------------------------------

test('every Bloodbank dropdown renders <Group> · <Label> (<value>)', () => {
  const lists = [
    property(new BloodbankTrigger(), 'events').options,
    property(new BloodbankTrigger(), 'command').options,
    property(new Bloodbank(), 'event').options,
    property(new Bloodbank(), 'command').options,
    property(new Fleet(), 'providerEventGuard').options,
  ];
  for (const options of lists) {
    assert.ok(options.length > 0);
    for (const option of options) {
      assert.match(option.name, OPTION_SHAPE, option.name);
      assert.ok(option.name.endsWith(`(${option.value})`), option.name);
    }
  }
});

test('the canonical example renders exactly as specified', () => {
  const events = property(new BloodbankTrigger(), 'events').options;
  const created = events.find((option) => option.value === 'bloodbank.repo.task.created');
  assert.equal(created.name, 'Repo · On Task Created (bloodbank.repo.task.created)');
});

test('provider aliases sit right after their canonical event and say what they filter', () => {
  const events = property(new BloodbankTrigger(), 'events').options;
  const index = events.findIndex((option) => option.value === 'bloodbank.repo.task.created');
  const alias = events[index + 1];
  assert.equal(alias.value, 'plane.ticket.created');
  assert.equal(alias.name, 'Plane · On Ticket Created (plane.ticket.created)');
  assert.match(alias.description, /^Filters Repo · On Task Created to provider=plane/);

  const updated = events.findIndex((option) => option.value === 'bloodbank.repo.task.updated');
  assert.deepEqual(
    events.slice(updated + 1, updated + 4).map((option) => option.value),
    ['plane.ticket.updated', 'plane.ticket.transitioned', 'plane.ticket.deleted'],
  );
});

test('labels fix acronyms and descriptions drop naming-contract boilerplate', () => {
  const byType = Object.fromEntries(eventSchemas.map((schema) => [schema.type, schema]));
  assert.equal(byType['bloodbank.cli.session.started'].group, 'CLI');
  assert.equal(byType['bloodbank.llm.request.sent'].group, 'LLM');
  for (const schema of eventSchemas) {
    assert.doesNotMatch(schema.description, /See bloodbank\/docs\/event-naming\.md/);
  }
  assert.equal(byType['bloodbank.agent.invocation.skipped'].label, 'On Invocation Skipped');
});

test('the publisher lists required data in the description, the trigger does not', () => {
  const publisher = property(new Bloodbank(), 'event').options.find((option) => option.value === 'bloodbank.repo.task.created');
  assert.match(publisher.description, /Data requires: .*provider_event_type/);
  const trigger = property(new BloodbankTrigger(), 'events').options.find((option) => option.value === 'bloodbank.repo.task.created');
  assert.doesNotMatch(trigger.description, /Data requires/);
});

// ---------------------------------------------------------------------------
// Only When Data Matches
// ---------------------------------------------------------------------------

const STARTED = {
  type: 'bloodbank.agent.invocation.started',
  kind: 'event',
  data: { invocation_id: 'i-1', context: { reason: 'ticket-grooming', labels: ['a', 'b'] } },
};

test('data conditions read the fixedCollection and dot paths into the envelope', () => {
  const conditions = parseDataConditions({
    conditions: [
      { path: 'data.context.reason', values: 'ticket-grooming, ticket-delegation' },
      { path: '  ', values: 'ignored' },
      { path: '$json.type', values: '' },
    ],
  });
  assert.deepEqual(conditions, [
    { path: 'data.context.reason', values: ['ticket-grooming', 'ticket-delegation'] },
    { path: 'type', values: [] },
  ]);
  assert.equal(valueAtPath(STARTED, 'data.context.reason'), 'ticket-grooming');
  assert.equal(valueAtPath(STARTED, 'data.context.labels.1'), 'b');
  assert.equal(valueAtPath(STARTED, 'data.nope.deeper'), undefined);
  assert.equal(matchesDataConditions(STARTED, conditions), true);
  assert.equal(matchesDataConditions(STARTED, parseDataConditions({})), true);
});

test('a message outside the data filter is refused before it can become an execution', () => {
  const pickup = parseDataConditions({ conditions: [{ path: 'data.context.reason', values: 'ticket-grooming,ticket-delegation' }] });
  const bindings = ['bloodbank.agent.invocation.started', 'bloodbank.agent.invocation.completed'];
  assert.equal(triggerAccepts('event', bindings, pickup, STARTED), true);
  const sessionTurn = { ...STARTED, data: { invocation_id: 'i-2' } };
  assert.equal(triggerAccepts('event', bindings, pickup, sessionTurn), false);
  const other = { ...STARTED, data: { invocation_id: 'i-3', context: { reason: 'fleet-invoke' } } };
  assert.equal(triggerAccepts('event', bindings, pickup, other), false);
  assert.equal(triggerAccepts('event', ['bloodbank.agent.invocation.failed'], [], STARTED), false);
});

test('arrays match on any element; an empty value list means present and non-empty', () => {
  assert.equal(matchesDataConditions(STARTED, [{ path: 'data.context.labels', values: ['b'] }]), true);
  assert.equal(matchesDataConditions(STARTED, [{ path: 'data.context.labels', values: ['z'] }]), false);
  assert.equal(matchesDataConditions(STARTED, [{ path: 'data.context.reason', values: [] }]), true);
  assert.equal(matchesDataConditions(STARTED, [{ path: 'data.context.missing', values: [] }]), false);
});

test('provider aliases match only their own provenance', () => {
  const created = {
    type: 'bloodbank.repo.task.created',
    data: { provider: 'plane', provider_event_type: 'plane.ticket.created' },
  };
  assert.equal(bindingMatches('plane.ticket.created', created), true);
  assert.equal(bindingMatches('bloodbank.repo.task.created', created), true);
  assert.equal(bindingMatches('plane.ticket.updated', created), false);
  assert.equal(bindingMatches('plane.ticket.created', { ...created, data: { provider: 'linear', provider_event_type: 'plane.ticket.created' } }), false);
});

// ---------------------------------------------------------------------------
// Replay: newest retained match via direct get
// ---------------------------------------------------------------------------

/** A fake stream: seq -> {subject, envelope}; answers last_by_subj / next_by_subj. */
function fakeStream(messages) {
  const rows = Object.entries(messages).map(([seq, row]) => ({ seq: Number(seq), ...row })).sort((a, b) => a.seq - b.seq);
  let requests = 0;
  const get = async (_stream, request) => {
    requests += 1;
    if (request.last_by_subj) {
      const found = rows.filter((row) => row.subject === request.last_by_subj).pop();
      return found ? { seq: found.seq, subject: found.subject, envelope: found.envelope } : null;
    }
    const found = rows.find((row) => row.seq >= request.seq && row.subject === request.next_by_subj);
    return found ? { seq: found.seq, subject: found.subject, envelope: found.envelope } : null;
  };
  return { get, requests: () => requests };
}

const SUBJECT = 'bloodbank.evt.agent.invocation.started';
const hit = (reason) => ({ type: 'bloodbank.agent.invocation.started', data: { context: { reason } } });

test('replay returns the newest message when it already matches', async () => {
  const stream = fakeStream({ 10: { subject: SUBJECT, envelope: hit('ticket-grooming') } });
  const found = await findLatestMatching(stream.get, 'S', SUBJECT, () => true);
  assert.equal(found.seq, 10);
  assert.equal(stream.requests(), 1);
});

test('replay walks back through growing windows to the newest match', async () => {
  const messages = { 5: { subject: SUBJECT, envelope: hit('ticket-grooming') } };
  for (let seq = 6; seq < 40_000; seq += 997) messages[seq] = { subject: SUBJECT, envelope: hit('session') };
  messages[3] = { subject: SUBJECT, envelope: hit('ticket-grooming') };
  messages[39_999] = { subject: 'bloodbank.evt.other.thing.happened', envelope: {} };
  const stream = fakeStream(messages);
  const found = await findLatestMatching(
    stream.get,
    'S',
    SUBJECT,
    (envelope) => envelope.data.context.reason === 'ticket-grooming',
    { initialWindow: 1000 },
  );
  assert.equal(found.seq, 5, 'the newest match, not the oldest');
});

test('replay gives up cleanly when nothing matches or nothing exists', async () => {
  const stream = fakeStream({ 7: { subject: SUBJECT, envelope: hit('session') } });
  assert.equal(await findLatestMatching(stream.get, 'S', SUBJECT, () => false), null);
  assert.equal(await findLatestMatching(stream.get, 'S', 'bloodbank.evt.none.none.none', () => true), null);
  const capped = fakeStream(Object.fromEntries(Array.from({ length: 50 }, (_, i) => [i + 1, { subject: SUBJECT, envelope: hit('session') }])));
  assert.equal(await findLatestMatching(capped.get, 'S', SUBJECT, () => false, { maxRequests: 5 }), null);
  assert.ok(capped.requests() <= 5);
});

test('a manual test replays the newest match across every bound subject', async () => {
  const conditions = parseDataConditions({ conditions: [{ path: 'data.context.reason', values: 'ticket-grooming' }] });
  const replay = async (_stream, subject, accepts) => {
    const envelope = { type: subject.replace('bloodbank.evt.', 'bloodbank.'), kind: 'event', data: { context: { reason: 'ticket-grooming' } } };
    if (!accepts(envelope)) return null;
    return { seq: subject.endsWith('completed') ? 20 : 10, subject, envelope };
  };
  const result = await manualTestEnvelope(
    'event',
    ['bloodbank.agent.invocation.started', 'bloodbank.agent.invocation.completed'],
    conditions,
    'replay',
    replay,
  );
  assert.equal(result.origin, 'replay');
  assert.equal(result.stored.seq, 20);
  assert.equal(result.envelope.type, 'bloodbank.agent.invocation.completed');
});

test('with nothing to replay, or no bus, a manual test falls back to a schema sample', async () => {
  const none = await manualTestEnvelope('event', ['plane.ticket.created'], [], 'replay', async () => null);
  assert.equal(none.origin, 'sample');
  assert.equal(none.envelope.sample, true);
  const down = await manualTestEnvelope('event', ['plane.ticket.created'], [], 'replay', async () => {
    throw new Error('ECONNREFUSED');
  });
  assert.equal(down.origin, 'sample');
  assert.match(down.note, /ECONNREFUSED/);
});

test('generated samples are schema-valid and pass the alias they were made for', () => {
  const created = sampleEnvelope('plane.ticket.created', 'event');
  assert.equal(created.type, 'bloodbank.repo.task.created');
  assert.equal(created.data.provider, 'plane');
  assert.equal(created.data.provider_event_type, 'plane.ticket.created');
  assert.ok(bindingMatches('plane.ticket.created', created));
  for (const type of ['bloodbank.repo.task.created', 'bloodbank.agent.invocation.skipped', 'bloodbank.system.heartbeat.received']) {
    const envelope = sampleEnvelope(type, 'event');
    assert.doesNotThrow(() => validateEnvelope(type, envelope), type);
  }
  const command = sampleEnvelope('bloodbank.agent.invocation.start', 'command');
  assert.equal(command.kind, 'command');
  assert.doesNotThrow(() => validateEnvelope('bloodbank.agent.invocation.start', command));
});

// ---------------------------------------------------------------------------
// The trigger in manual mode
// ---------------------------------------------------------------------------

function triggerContext(parameters, mode) {
  const emitted = [];
  const context = {
    getMode: () => mode,
    getNodeParameter(name, fallback) {
      return Object.prototype.hasOwnProperty.call(parameters, name) ? parameters[name] : fallback;
    },
    getNode: () => ({ name: 'Bloodbank Trigger', type: 'n8n-nodes-bloodbank.bloodbankTrigger', typeVersion: 1, parameters: {} }),
    emit: (data) => emitted.push(data),
    emitError: (error) => { throw error; },
    saveFailedExecution: () => {},
  };
  return { context, emitted };
}

test('a manual test with Generated Sample emits without subscribing to anything', async () => {
  const { context, emitted } = triggerContext(
    { messageKind: 'event', events: ['plane.ticket.transitioned'], testEventSource: 'sample', connection: { natsHost: 'nats.invalid', natsPort: 1 } },
    'manual',
  );
  const response = await BloodbankTrigger.prototype.trigger.call(context);
  assert.equal(typeof response.manualTriggerFunction, 'function');
  await response.manualTriggerFunction();
  assert.equal(emitted.length, 1);
  const item = emitted[0][0][0].json;
  assert.equal(item.data.provider_event_type, 'plane.ticket.transitioned');
  await response.closeFunction();
});

test('the trigger exposes the test source and data filter parameters', () => {
  const trigger = new BloodbankTrigger();
  const source = property(trigger, 'testEventSource');
  assert.deepEqual(source.options.map((option) => option.value), ['replay', 'sample', 'live']);
  assert.equal(source.default, 'replay');
  const match = property(trigger, 'dataMatch');
  assert.equal(match.type, 'fixedCollection');
  assert.deepEqual(match.options[0].values.map((value) => value.name), ['path', 'values']);
});

test('an unknown binding is refused at activation, not at the first message', async () => {
  const { context } = triggerContext({ messageKind: 'event', events: ['plane.ticket.exploded'] }, 'trigger');
  await assert.rejects(() => BloodbankTrigger.prototype.trigger.call(context), /unknown Bloodbank event binding/);
});
