const assert = require('node:assert/strict');
const { mkdtemp, rm, writeFile } = require('node:fs/promises');
const { join } = require('node:path');
const test = require('node:test');
const { stringify: stringifyYaml } = require('yaml');

const {
  Bloodbank,
  commandSchemas,
  publish,
  resolveFleetTargetForRepo,
} = require('../src/index.ts');

const COMMAND_TYPE = 'bloodbank.agent.invocation.start';
const COMMAND_ID = '550e8400-e29b-41d4-a716-446655440000';

function route(profileName, enabled = true, targetAgentId = 'bloodbank-pm') {
  return {
    repo: 'bloodbank',
    profile_name: profileName,
    bloodbank: {
      enabled,
      gateway_scope: 'fleet',
      target_agent_id: targetAgentId,
    },
  };
}

function registry(agents) {
  return { schema_version: 1, agents };
}

async function writeRegistry(t, value) {
  const dir = await mkdtemp(join(__dirname, '..', 'node_modules', '.publisher-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const file = join(dir, 'agents-registry.yaml');
  await writeFile(file, stringifyYaml(value), 'utf8');
  return file;
}

function executionContext(parameters, input = [{ json: { source: 'test' } }]) {
  return {
    getInputData: () => input,
    getNodeParameter(name, _index, fallback) {
      return Object.prototype.hasOwnProperty.call(parameters, name)
        ? parameters[name]
        : fallback;
    },
    getNode: () => ({
      id: 'publisher-node-test',
      name: 'Bloodbank',
      type: 'n8n-nodes-bloodbank.bloodbank',
      typeVersion: 1,
      position: [0, 0],
      parameters: {},
    }),
    continueOnFail: () => false,
  };
}

function commandParameters(registryFile, overrides = {}) {
  return {
    mode: 'command',
    command: COMMAND_TYPE,
    repository: 'bloodbank',
    prompt: 'Review the current Bloodbank queue.',
    context: '{"priority":"normal"}',
    threadId: 'thread-1',
    turnId: 'turn-1',
    identity: { commandId: COMMAND_ID },
    registryFile,
    connection: { natsHost: 'test-nats', natsPort: 4222, timeoutMs: 50 },
    ...overrides,
  };
}

function capturedPublisher(messages, connections = []) {
  return (options) => publish(options, async (connectionOptions) => {
    connections.push(connectionOptions);
    return {
      publish(subject, data) {
        messages.push({
          subject,
          envelope: JSON.parse(Buffer.from(data).toString('utf8')),
        });
      },
      flush: async () => {},
      drain: async () => {},
    };
  });
}

function assertPublisherInjectionSeam(node) {
  assert.equal(node.execute.length, 1, 'publisher node must accept its test transport seam');
}

test('repository routing returns exactly one eligible fleet target, never its profile', () => {
  const target = resolveFleetTargetForRepo(registry({
    'bloodbank-pm': route('private-runtime-profile'),
    research: { ...route('research-profile', true, 'research'), repo: 'research' },
  }), 'bloodbank');

  assert.equal(target, 'bloodbank-pm');
  assert.notEqual(target, 'private-runtime-profile');
});

const rejectedRoutes = [
  [
    'an absent repository',
    registry({ research: { ...route('research-profile', true, 'research'), repo: 'research' } }),
    /no registry route/,
  ],
  [
    'a disabled route',
    registry({ 'bloodbank-pm': route('bloodbank-profile', false) }),
    /not eligible/,
  ],
  [
    'a target mismatch',
    registry({ 'bloodbank-pm': route('bloodbank-profile', true, 'other-agent') }),
    /not eligible/,
  ],
  [
    'ambiguous eligible routes',
    registry({
      'bloodbank-pm': route('bloodbank-profile'),
      'bloodbank-ops': route('operations-profile', true, 'bloodbank-ops'),
    }),
    /ambiguous/,
  ],
];

for (const [label, value, pattern] of rejectedRoutes) {
  test(`repository routing fails closed for ${label}`, () => {
    assert.throws(() => resolveFleetTargetForRepo(value, 'bloodbank'), pattern);
  });
}

test('repository routing rejects malformed canonical registry snapshots', () => {
  assert.throws(
    () => resolveFleetTargetForRepo({ agents: {} }, 'bloodbank'),
    /schema_version/,
  );
  assert.throws(
    () => resolveFleetTargetForRepo({ schema_version: 1, agents: [] }, 'bloodbank'),
    /agents must be a mapping/,
  );
  assert.throws(
    () => resolveFleetTargetForRepo(registry({ ' Bloodbank-PM ': route('profile') }), 'bloodbank'),
    /canonical lowercase slug/,
  );
});

test('publisher exposes explicit event/command mode backed by generated schemas', () => {
  const node = new Bloodbank();
  const mode = node.description.properties.find((property) => property.name === 'mode');
  const command = node.description.properties.find((property) => property.name === 'command');
  const profileInputs = node.description.properties.filter((property) =>
    /profile/i.test(String(property.name)),
  );
  const invocation = commandSchemas.find((schema) => schema.type === COMMAND_TYPE);

  assert.deepEqual(mode.options.map((option) => option.value), ['event', 'command']);
  assert.equal(mode.default, 'event');
  assert.deepEqual(command.options.map((option) => option.value), [COMMAND_TYPE]);
  assert.equal(profileInputs.length, 0);
  assert.equal(invocation.dataFields.find((field) => field.name === 'prompt').required, true);
});

test('command mode publishes one validated targeted invocation without exposing a profile', async (t) => {
  const registryFile = await writeRegistry(t, registry({
    'bloodbank-pm': route('private-runtime-profile'),
  }));
  const messages = [];
  const connections = [];
  const node = new Bloodbank();
  assertPublisherInjectionSeam(node);

  const result = await node.execute.call(
    executionContext(commandParameters(registryFile)),
    capturedPublisher(messages, connections),
  );

  assert.equal(messages.length, 1);
  assert.equal(connections.length, 1);
  const published = messages[0];
  assert.equal(published.subject, 'bloodbank.cmd.agent.invocation.start');
  assert.equal(published.envelope.kind, 'command');
  assert.equal(published.envelope.delivery, 'single_consumer');
  assert.equal(published.envelope.command_id, COMMAND_ID);
  assert.equal(published.envelope.correlationid, COMMAND_ID);
  assert.equal(published.envelope.causationid, null);
  assert.equal(
    published.envelope.idempotency_key,
    `agent.invocation.start:target:bloodbank-pm:command:${COMMAND_ID}`,
  );
  assert.equal(published.envelope.data.target_agent_id, 'bloodbank-pm');
  assert.equal(published.envelope.data.prompt, 'Review the current Bloodbank queue.');
  assert.deepEqual(published.envelope.data.context, { priority: 'normal' });
  assert.equal('profile_name' in published.envelope.data, false);
  assert.equal(JSON.stringify(published.envelope).includes('private-runtime-profile'), false);
  assert.deepEqual(result[0][0].json, {
    published: true,
    kind: 'command',
    type: COMMAND_TYPE,
    subject: 'bloodbank.cmd.agent.invocation.start',
    eventId: COMMAND_ID,
    commandId: COMMAND_ID,
    correlationid: COMMAND_ID,
    repository: 'bloodbank',
    targetAgentId: 'bloodbank-pm',
  });
});

for (const [label, value, pattern] of rejectedRoutes) {
  test(`command mode makes zero publish attempts for ${label}`, async (t) => {
    const registryFile = await writeRegistry(t, value);
    let publishCalls = 0;
    const node = new Bloodbank();
    assertPublisherInjectionSeam(node);

    await assert.rejects(
      node.execute.call(
        executionContext(commandParameters(registryFile)),
        async () => {
          publishCalls += 1;
          throw new Error('publisher must not be called');
        },
      ),
      pattern,
    );
    assert.equal(publishCalls, 0);
  });
}

test('command mode makes zero publish attempts for a missing prompt', async (t) => {
  const registryFile = await writeRegistry(t, registry({
    'bloodbank-pm': route('private-runtime-profile'),
  }));
  let publishCalls = 0;
  const node = new Bloodbank();
  assertPublisherInjectionSeam(node);

  await assert.rejects(
    node.execute.call(
      executionContext(commandParameters(registryFile, { prompt: '   ' })),
      async () => {
        publishCalls += 1;
        throw new Error('publisher must not be called');
      },
    ),
    /prompt/i,
  );
  assert.equal(publishCalls, 0);
});

test('command mode requires retry-stable command identity before publishing', async (t) => {
  const registryFile = await writeRegistry(t, registry({
    'bloodbank-pm': route('private-runtime-profile'),
  }));
  let publishCalls = 0;
  const node = new Bloodbank();

  await assert.rejects(
    node.execute.call(
      executionContext(commandParameters(registryFile, { identity: {} })),
      async () => {
        publishCalls += 1;
        throw new Error('publisher must not be called');
      },
    ),
    /command ID/i,
  );
  assert.equal(publishCalls, 0);
});

test('event mode preserves the existing envelope, subject, and output shape', async () => {
  const messages = [];
  const node = new Bloodbank();
  assertPublisherInjectionSeam(node);
  const result = await node.execute.call(executionContext({
    mode: 'event',
    event: 'bloodbank.repo.task.created',
    data: { repo: '33GOD', task_id: 'ticket-1', title: 'Test' },
    connection: { natsHost: 'test-nats', natsPort: 4222 },
  }), capturedPublisher(messages));

  assert.equal(messages.length, 1);
  assert.equal(messages[0].subject, 'bloodbank.evt.repo.task.created');
  assert.equal(messages[0].envelope.kind, 'event');
  assert.equal(messages[0].envelope.ordering_key, 'repo:ticket-1');
  assert.equal('command_id' in messages[0].envelope, false);
  assert.deepEqual(Object.keys(result[0][0].json).sort(), [
    'correlationid',
    'eventId',
    'published',
    'subject',
    'type',
  ]);
  assert.equal(result[0][0].json.subject, 'bloodbank.evt.repo.task.created');
});
