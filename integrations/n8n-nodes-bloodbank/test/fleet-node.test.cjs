const assert = require('node:assert/strict');
const { mkdtemp, rm, writeFile } = require('node:fs/promises');
const { join } = require('node:path');
const test = require('node:test');
const { stringify: stringifyYaml } = require('yaml');

const {
  Fleet,
  bloodbankActivation,
  delegationPrompt,
  deterministicUuid,
  executionMode,
  fleetCommandId,
  groomingPrompt,
  hermesRegistryPath,
  publish,
  resolveFleetAgentForBoard,
  resolveFleetTargetForRepo,
  ticketCorrelationId,
  ticketFactsFromEnvelope,
  validateEnvelope,
} = require('../src/index.ts');

const BOARD = 'a8a12be1-b3ab-44f4-ab24-abe8829aeb72';
const OTHER_BOARD = '15258893-0206-4e8f-aea6-340eb217988c';
const TICKET_ID = '5082ee4f-5e93-4fd5-8ee9-62ea4109b7fd';
const COMMAND = 'bloodbank.agent.invocation.start';
const SKIPPED = 'bloodbank.agent.invocation.skipped';

function agent(overrides = {}) {
  return {
    repo: 'james-brennan',
    profile_name: 'james-brennan-pm',
    project_path: '/nonexistent/james-brennan',
    plane: { identifier: 'JIMB', project_id: BOARD, workspace: 'automaticai' },
    bloodbank: { enabled: true, gateway_scope: 'fleet', target_agent_id: 'james-brennan-pm' },
    ...overrides,
  };
}

function registry(agents) {
  return { schema_version: 1, agents };
}

const LIVE_REGISTRY = registry({
  'james-brennan-pm': agent(),
  '33god-pm': {
    repo: '33god',
    profile_name: '33god-pm',
    plane: { identifier: 'GOD', project_id: OTHER_BOARD, workspace: '33god' },
    bloodbank: { enabled: false, gateway_scope: 'fleet', target_agent_id: '33god-pm' },
  },
});

function envelope(data, extra = {}) {
  return {
    specversion: '1.0',
    id: '11111111-2222-4333-8444-555555555555',
    type: 'bloodbank.repo.task.created',
    kind: 'event',
    data,
    ...extra,
  };
}

const CREATED = envelope({
  repo: 'james-brennan',
  board_id: BOARD,
  ticket_key: 'JIMB-273',
  title: 'Wire the delegation lane',
  workspace: 'automaticai',
  provider_event_type: 'plane.ticket.created',
  phase: 'Backlog',
});

const MOVED_TO_TODO = envelope({
  repo: 'james-brennan',
  board_id: BOARD,
  ticket_key: 'JIMB-273',
  title: 'Wire the delegation lane',
  workspace: 'automaticai',
  provider_event_type: 'plane.ticket.transitioned',
  previous_phase: 'Backlog',
  phase: 'Todo',
});

async function tempDir(t, prefix) {
  const dir = await mkdtemp(join(__dirname, '..', 'node_modules', prefix));
  t.after(() => rm(dir, { recursive: true, force: true }));
  return dir;
}

async function writeRegistry(t, value) {
  const dir = await tempDir(t, '.fleet-test-');
  const file = join(dir, 'agents-registry.yaml');
  await writeFile(file, stringifyYaml(value), 'utf8');
  return file;
}

function executionContext(parameters, input, { continueOnFail = false } = {}) {
  return {
    getInputData: () => input,
    getNodeParameter(name, _index, fallback) {
      return Object.prototype.hasOwnProperty.call(parameters, name) ? parameters[name] : fallback;
    },
    getNode: () => ({
      id: 'fleet-node-test',
      name: '33GOD Agent Fleet',
      type: 'n8n-nodes-bloodbank.bloodbankFleet',
      typeVersion: 1,
      position: [0, 0],
      parameters: {},
    }),
    continueOnFail: () => continueOnFail,
  };
}

/** Records every publish; commands and skip events are separated by type. */
function recorder({ failSkips = false } = {}) {
  const calls = [];
  return {
    calls,
    commands: () => calls.filter((call) => call.type === COMMAND),
    skips: () => calls.filter((call) => call.type === SKIPPED),
    async send(options) {
      calls.push(options);
      if (failSkips && options.type === SKIPPED) throw new Error('bus down');
      return {
        subject: options.type === COMMAND
          ? 'bloodbank.cmd.agent.invocation.start'
          : 'bloodbank.evt.agent.invocation.skipped',
        correlationid: options.correlationId,
        eventId: options.eventId || 'event-id',
        commandId: options.commandId,
      };
    },
  };
}

/** The real publisher over a fake NATS connection: schema validation runs. */
function capturedPublisher(messages) {
  return (options) => publish(options, async () => ({
    publish(subject, data) {
      messages.push({ subject, envelope: JSON.parse(Buffer.from(data).toString('utf8')) });
    },
    flush: async () => {},
    drain: async () => {},
  }));
}

async function run(t, parameters, input, registryValue = LIVE_REGISTRY, options = {}) {
  const registryFile = await writeRegistry(t, registryValue);
  const rec = options.recorder || recorder();
  const context = executionContext({ registryFile, ...parameters }, input, options);
  const [dispatched, skipped] = await Fleet.prototype.execute.call(context, options.send || rec.send);
  return { dispatched, skipped, rec };
}

// ---------------------------------------------------------------------------
// Resolution and eligibility
// ---------------------------------------------------------------------------

test('the board id resolves the owning agent, not the repo slug', () => {
  const route = resolveFleetAgentForBoard(LIVE_REGISTRY, BOARD, '');
  assert.equal(route.agentId, 'james-brennan-pm');
  assert.equal(route.eligible, true);
  assert.equal(route.matchedBy, 'board');
  assert.equal(route.projectPath, '/nonexistent/james-brennan');
  assert.equal(route.workspace, 'automaticai');
});

test('an unregistered board falls back to <repo>-pm by convention', () => {
  const route = resolveFleetAgentForBoard(LIVE_REGISTRY, 'ffffffff-0000-4000-8000-000000000000', 'james-brennan');
  assert.equal(route.agentId, 'james-brennan-pm');
  assert.equal(route.matchedBy, 'fallback');
  assert.equal(route.eligible, true);
});

test('a switched-off project is ineligible with a readable reason, never a throw', () => {
  const route = resolveFleetAgentForBoard(LIVE_REGISTRY, OTHER_BOARD, '33god');
  assert.equal(route.eligible, false);
  assert.equal(route.code, 'ineligible');
  assert.match(route.why, /bloodbank\.enabled is false/);
  assert.equal(route.agentId, '33god-pm');
});

test('no key defaults to enabled: an ABSENT bloodbank.enabled is eligible', () => {
  const route = resolveFleetAgentForBoard(
    registry({ 'james-brennan-pm': agent({ bloodbank: { gateway_scope: 'fleet', target_agent_id: 'james-brennan-pm' } }) }),
    BOARD,
    'james-brennan',
  );
  assert.equal(route.eligible, true, route.why);
  assert.equal(bloodbankActivation({}), 'enabled');
  assert.equal(bloodbankActivation({ enabled: true }), 'enabled');
  assert.equal(bloodbankActivation({ enabled: false }), 'disabled');
});

test('a present non-boolean bloodbank.enabled is invalid, not guessed', () => {
  for (const value of ['true', 'yes', null, 1, 0]) {
    const route = resolveFleetAgentForBoard(
      registry({ 'james-brennan-pm': agent({ bloodbank: { enabled: value, gateway_scope: 'fleet', target_agent_id: 'james-brennan-pm' } }) }),
      BOARD,
      'james-brennan',
    );
    assert.equal(route.eligible, false, `enabled=${JSON.stringify(value)}`);
    assert.equal(route.code, 'invalid_policy');
    assert.match(route.why, /invalid bloodbank\.enabled/);
    assert.equal(bloodbankActivation({ enabled: value }), 'invalid');
  }
});

test('the publisher repo router applies the same no-key-means-enabled rule', () => {
  const row = { repo: 'bloodbank', profile_name: 'p', bloodbank: { gateway_scope: 'fleet', target_agent_id: 'bloodbank-pm' } };
  assert.equal(resolveFleetTargetForRepo(registry({ 'bloodbank-pm': row }), 'bloodbank'), 'bloodbank-pm');
  assert.throws(
    () => resolveFleetTargetForRepo(registry({ 'bloodbank-pm': { ...row, bloodbank: { ...row.bloodbank, enabled: 'true' } } }), 'bloodbank'),
    /not eligible/,
  );
});

test('each of the four gateway eligibility conditions is reported distinctly', () => {
  const cases = [
    [{ profile_name: '' }, /no profile_name/],
    [{ bloodbank: undefined }, /no bloodbank block/],
    [{ bloodbank: { enabled: true, gateway_scope: 'agent', target_agent_id: 'james-brennan-pm' } }, /gateway_scope is not 'fleet'/],
    [{ bloodbank: { enabled: true, gateway_scope: 'fleet', target_agent_id: 'someone-else' } }, /target_agent_id mismatch/],
  ];
  for (const [overrides, expected] of cases) {
    const route = resolveFleetAgentForBoard(
      registry({ 'james-brennan-pm': agent(overrides) }),
      BOARD,
      'james-brennan',
    );
    assert.equal(route.eligible, false);
    assert.match(route.why, expected);
  }
});

test('an unknown board with no fallback is a no_route skip rather than an error', () => {
  const route = resolveFleetAgentForBoard(registry({}), BOARD, 'nobody');
  assert.equal(route.eligible, false);
  assert.equal(route.code, 'no_route');
  assert.match(route.why, /no registry entry matches board/);
});

// ---------------------------------------------------------------------------
// Correlation and idempotency
// ---------------------------------------------------------------------------

test('a ticket correlation id is byte-identical to the Python publisher it replaces', () => {
  // Recorded from the live bb-triage-invoke run that groomed JIMB-273.
  assert.equal(ticketCorrelationId(BOARD, 'JIMB-273'), 'dda43316-59d5-52d8-a66d-4295c9ed97fe');
});

test('correlation is per ticket, so two tickets are two conversations', () => {
  assert.notEqual(ticketCorrelationId(BOARD, 'JIMB-273'), ticketCorrelationId(BOARD, 'JIMB-274'));
  assert.equal(ticketCorrelationId(BOARD, 'JIMB-273'), ticketCorrelationId(BOARD, 'JIMB-273'));
});

test('without an inherited correlation id the fleet derives the webhook\'s own (board:ticket_id)', async (t) => {
  const { rec } = await run(t, { operation: 'groomTicket' }, [
    { json: envelope({ ...CREATED.data, ticket_id: TICKET_ID }) },
  ]);
  const webhookDerivation = deterministicUuid(`plane:${BOARD}:${TICKET_ID}`);
  assert.equal(rec.commands()[0].correlationId, webhookDerivation);
  assert.equal(ticketCorrelationId(BOARD, TICKET_ID), webhookDerivation);
});

test('the command id is a pure function of the causing event, so a redelivery dedups', async (t) => {
  const first = await run(t, { operation: 'groomTicket' }, [{ json: CREATED }]);
  const again = await run(t, { operation: 'groomTicket' }, [{ json: CREATED }]);
  const other = await run(t, { operation: 'groomTicket' }, [
    { json: { ...CREATED, id: '99999999-2222-4333-8444-555555555555' } },
  ]);
  const commandId = first.rec.commands()[0].commandId;
  assert.equal(commandId, fleetCommandId(CREATED.id, 'groomTicket', 'james-brennan-pm'));
  assert.equal(again.rec.commands()[0].commandId, commandId);
  assert.notEqual(other.rec.commands()[0].commandId, commandId);
  assert.equal(first.dispatched[0].json.commandId, commandId);
});

test('a redelivered event yields the same idempotency_key on the wire', async (t) => {
  const messages = [];
  const send = capturedPublisher(messages);
  await run(t, { operation: 'groomTicket' }, [{ json: CREATED }], LIVE_REGISTRY, { send });
  await run(t, { operation: 'groomTicket' }, [{ json: CREATED }], LIVE_REGISTRY, { send });
  const commands = messages.filter((message) => message.envelope.type === COMMAND);
  assert.equal(commands.length, 2);
  assert.equal(commands[0].envelope.command_id, commands[1].envelope.command_id);
  assert.equal(commands[0].envelope.idempotency_key, commands[1].envelope.idempotency_key);
  assert.match(commands[0].envelope.idempotency_key, /^agent\.invocation\.start:target:james-brennan-pm:command:/);
  assert.doesNotThrow(() => validateEnvelope(COMMAND, commands[0].envelope));
});

test('with no causing event id the publisher mints a fresh command id', async (t) => {
  const { rec } = await run(t, { operation: 'groomTicket' }, [{ json: { data: CREATED.data } }]);
  assert.equal(rec.commands()[0].commandId, undefined);
});

// ---------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------

test('ticket facts read a whole envelope or a bare data object alike', () => {
  const fromEnvelope = ticketFactsFromEnvelope(MOVED_TO_TODO);
  const fromData = ticketFactsFromEnvelope(MOVED_TO_TODO.data);
  assert.deepEqual(fromEnvelope, fromData);
  assert.equal(fromEnvelope.ticketKey, 'JIMB-273');
  assert.equal(fromEnvelope.phase, 'Todo');
  assert.equal(fromEnvelope.previousPhase, 'Backlog');
});

test('the grooming prompt carries the two facts an agent cannot derive', () => {
  const prompt = groomingPrompt(ticketFactsFromEnvelope(CREATED), '/home/delorenj/code/james-brennan');
  assert.match(prompt, /Repo checkout: \/home\/delorenj\/code\/james-brennan/);
  assert.match(prompt, new RegExp(`Plane board id: ${BOARD}`));
  assert.match(prompt, /project_id/);
  assert.match(prompt, /do NOT create, split, or decompose/i);
  assert.match(prompt, /lifecycle:triaged/);
});

test('the grooming prompt stamps nothing when the completion label is cleared', () => {
  const prompt = groomingPrompt(ticketFactsFromEnvelope(CREATED), '', '');
  assert.doesNotMatch(prompt, /lifecycle:triaged/);
  assert.match(prompt, new RegExp(`Plane board id: ${BOARD}`));
  assert.doesNotMatch(prompt, /Repo checkout/);
});

test('with no board id and no checkout path there is nothing to point at', () => {
  const prompt = groomingPrompt(ticketFactsFromEnvelope(envelope({ repo: 'x', ticket_key: 'X-1' })), '', '');
  assert.doesNotMatch(prompt, /Where things are/);
});

test('the delegation prompt gates on the groomed label and states the board rules', () => {
  const prompt = delegationPrompt(ticketFactsFromEnvelope(MOVED_TO_TODO), '/home/delorenj/code/james-brennan');
  assert.match(prompt, /lifecycle:triaged/);
  assert.match(prompt, /Do not delegate a ticket nobody has groomed/);
  assert.match(prompt, /In Progress/);
  assert.match(prompt, /start date/);
  assert.match(prompt, /assignee empty/);
  assert.match(prompt, /you do not write the code yourself/);
});

// ---------------------------------------------------------------------------
// Node: dispatch
// ---------------------------------------------------------------------------

test('the node declares Dispatched and Skipped outputs and no retired UI parameters', () => {
  const { description } = new Fleet();
  assert.deepEqual(description.outputs, ['main', 'main']);
  assert.deepEqual(description.outputNames, ['Dispatched', 'Skipped']);
  const byName = Object.fromEntries(description.properties.map((property) => [property.name, property]));
  for (const retired of ['registryFile', 'onIneligible', 'ticket']) {
    assert.equal(byName[retired].type, 'hidden', `${retired} must be hidden, not removed`);
  }
  assert.equal(byName.providerEventGuard.type, 'multiOptions');
  assert.ok(byName.providerEventGuard.options.some((option) => option.value === 'plane.ticket.created'));
  assert.equal(byName.repo.default, '={{ $json.data?.repo }}');
  assert.equal(byName.boardId.default, '={{ $json.data?.board_id ?? $json.data?.project_id }}');
  assert.equal(byName.correlationId.default, '={{ $json.correlationid }}');
  assert.equal(byName.causationId.default, '={{ $json.id }}');
});

test('groom publishes one invocation command addressed to the resolved agent', async (t) => {
  const { dispatched, skipped, rec } = await run(
    t,
    { operation: 'groomTicket', providerEventGuard: 'plane.ticket.created' },
    [{ json: CREATED }],
  );
  const calls = rec.commands();
  assert.equal(calls.length, 1);
  assert.equal(rec.skips().length, 0);
  assert.equal(calls[0].kind, 'command');
  assert.equal(calls[0].data.target_agent_id, 'james-brennan-pm');
  assert.equal(calls[0].correlationId, 'dda43316-59d5-52d8-a66d-4295c9ed97fe');
  assert.equal(calls[0].causationId, CREATED.id);
  assert.equal(calls[0].data.context.reason, 'ticket-grooming');
  assert.equal(calls[0].data.context.ticket_key, 'JIMB-273');
  assert.equal(dispatched.length, 1);
  assert.equal(skipped.length, 0);
  assert.equal(dispatched[0].json.invoked, true);
  assert.equal(dispatched[0].json.agentId, 'james-brennan-pm');
  assert.equal(dispatched[0].json.boardId, BOARD);
  assert.equal(dispatched[0].json.ticketKey, 'JIMB-273');
  assert.equal(dispatched[0].json.correlationid, 'dda43316-59d5-52d8-a66d-4295c9ed97fe');
});

test('delegate fires on a transition into Todo', async (t) => {
  const { dispatched, rec } = await run(
    t,
    { operation: 'delegateTicket', phaseGuard: 'Todo,unstarted' },
    [{ json: MOVED_TO_TODO }],
  );
  const calls = rec.commands();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].data.context.reason, 'ticket-delegation');
  assert.equal(calls[0].data.context.previous_phase, 'Backlog');
  assert.match(calls[0].data.prompt, /has moved into Todo/);
  assert.equal(dispatched[0].json.invoked, true);
});

test('invoke sends the caller prompt verbatim', async (t) => {
  const { rec } = await run(t, { operation: 'invoke', prompt: 'Report the current cycle.' }, [{ json: CREATED }]);
  assert.equal(rec.commands()[0].data.prompt, 'Report the current cycle.');
  assert.equal(rec.commands()[0].data.context.reason, 'fleet-invoke');
});

test('an inherited correlation id wins, so a caused command stays on its thread', async (t) => {
  const inherited = '9f9f9f9f-1111-4222-8333-444444444444';
  const { rec } = await run(t, { operation: 'groomTicket' }, [
    { json: envelope(CREATED.data, { correlationid: inherited }) },
  ]);
  assert.equal(rec.commands()[0].correlationId, inherited);
});

test('an item with neither repo nor board id is a hard error, not a silent skip', async (t) => {
  await assert.rejects(
    () => run(t, { operation: 'groomTicket' }, [{ json: envelope({ ticket_key: 'JIMB-1' }) }]),
    /neither data\.repo nor a board id/,
  );
});

// ---------------------------------------------------------------------------
// Node: mapping parameters
// ---------------------------------------------------------------------------

test('mapped parameters override the envelope, and blank ones fall back to lifting', async (t) => {
  const { rec } = await run(
    t,
    {
      operation: 'groomTicket',
      repo: '',
      boardId: 'undefined',
      ticketKey: 'JIMB-999',
      title: 'Overridden title',
    },
    [{ json: CREATED }],
  );
  const context = rec.commands()[0].data.context;
  assert.equal(context.repo, 'james-brennan');
  assert.equal(context.board_id, BOARD);
  assert.equal(context.ticket_key, 'JIMB-999');
  assert.equal(context.title, 'Overridden title');
});

test('mapped correlation and causation ids are honoured', async (t) => {
  const correlation = '7f7f7f7f-1111-4222-8333-444444444444';
  const causation = '6e6e6e6e-1111-4222-8333-444444444444';
  const { rec } = await run(t, { operation: 'groomTicket', correlationId: correlation, causationId: causation }, [
    { json: CREATED },
  ]);
  assert.equal(rec.commands()[0].correlationId, correlation);
  assert.equal(rec.commands()[0].causationId, causation);
  assert.equal(rec.commands()[0].commandId, fleetCommandId(causation, 'groomTicket', 'james-brennan-pm'));
});

test('the legacy Ticket collection still wins over the incoming envelope', async (t) => {
  const { rec } = await run(
    t,
    {
      operation: 'groomTicket',
      ticket: { boardId: BOARD, repo: 'james-brennan', ticketKey: 'JIMB-999' },
    },
    [{ json: { unrelated: true } }],
  );
  assert.equal(rec.commands()[0].data.context.ticket_key, 'JIMB-999');
  assert.equal(rec.commands()[0].correlationId, ticketCorrelationId(BOARD, 'JIMB-999'));
});

// ---------------------------------------------------------------------------
// Node: registry location
// ---------------------------------------------------------------------------

test('the registry path comes from a saved legacy value, then env, then the default', () => {
  assert.equal(hermesRegistryPath('/saved.yaml', { HERMES_AGENTS_REGISTRY: '/env.yaml' }), '/saved.yaml');
  assert.equal(hermesRegistryPath('', { HERMES_AGENTS_REGISTRY: '/env.yaml', HERMES_FLEET_REGISTRY_FILE: '/b.yaml' }), '/env.yaml');
  assert.equal(hermesRegistryPath('', { HERMES_FLEET_REGISTRY_FILE: '/b.yaml' }), '/b.yaml');
  assert.match(hermesRegistryPath('', {}), /\.hermes\/agents-registry\.yaml$/);
});

test('with no saved registry path the node reads HERMES_AGENTS_REGISTRY', async (t) => {
  const file = await writeRegistry(t, LIVE_REGISTRY);
  const previous = process.env.HERMES_AGENTS_REGISTRY;
  process.env.HERMES_AGENTS_REGISTRY = file;
  t.after(() => {
    if (previous === undefined) delete process.env.HERMES_AGENTS_REGISTRY;
    else process.env.HERMES_AGENTS_REGISTRY = previous;
  });
  const rec = recorder();
  const context = executionContext({ operation: 'groomTicket' }, [{ json: CREATED }]);
  const [dispatched] = await Fleet.prototype.execute.call(context, rec.send);
  assert.equal(dispatched[0].json.agentId, 'james-brennan-pm');
});

// ---------------------------------------------------------------------------
// Node: skips
// ---------------------------------------------------------------------------

test('an ineligible project leaves on Skipped and publishes agent.invocation.skipped', async (t) => {
  const { dispatched, skipped, rec } = await run(t, { operation: 'groomTicket' }, [
    { json: envelope({ repo: '33god', board_id: OTHER_BOARD, ticket_key: 'GOD-1', ticket_id: TICKET_ID, provider_event_type: 'plane.ticket.created' }) },
  ]);
  assert.equal(rec.commands().length, 0);
  assert.equal(dispatched.length, 0);
  assert.equal(skipped.length, 1);
  assert.equal(skipped[0].json.invoked, false);
  assert.equal(skipped[0].json.code, 'ineligible');
  assert.equal(skipped[0].json.agentId, '33god-pm');
  assert.equal(skipped[0].json.ticketKey, 'GOD-1');
  assert.match(skipped[0].json.reason, /bloodbank\.enabled is false/);

  const [event] = rec.skips();
  assert.equal(event.kind, 'event');
  assert.equal(event.validate, true);
  assert.equal(event.data.skip_code, 'ineligible');
  assert.equal(event.data.target_agent_id, '33god-pm');
  assert.equal(event.data.context.reason, 'ticket-grooming');
  assert.equal(event.data.context.board_id, OTHER_BOARD);
  assert.equal(event.data.context.provider_event_type, 'plane.ticket.created');
  assert.equal(event.causationId, CREATED.id);
  assert.equal(skipped[0].json.skipEvent.published, true);
});

test('the skip event is a schema-valid envelope on bloodbank.evt.agent.invocation.skipped', async (t) => {
  const messages = [];
  await run(t, { operation: 'groomTicket' }, [
    { json: envelope({ repo: '33god', board_id: OTHER_BOARD, ticket_key: 'GOD-1', ticket_id: TICKET_ID, provider_event_type: 'plane.ticket.created' }) },
  ], LIVE_REGISTRY, { send: capturedPublisher(messages) });
  assert.equal(messages.length, 1);
  assert.equal(messages[0].subject, 'bloodbank.evt.agent.invocation.skipped');
  assert.equal(messages[0].envelope.source, 'urn:33god:integration:n8n:agent-fleet');
  assert.equal(messages[0].envelope.ordering_key, `task:33god:${TICKET_ID}`);
  assert.doesNotThrow(() => validateEnvelope(SKIPPED, messages[0].envelope));
});

test('a failed skip publish keeps the item and says so', async (t) => {
  const rec = recorder({ failSkips: true });
  const { skipped } = await run(t, { operation: 'groomTicket' }, [
    { json: envelope({ repo: '33god', board_id: OTHER_BOARD, ticket_key: 'GOD-1', provider_event_type: 'plane.ticket.created' }) },
  ], LIVE_REGISTRY, { recorder: rec });
  assert.equal(skipped.length, 1);
  assert.deepEqual(skipped[0].json.skipEvent, { published: false, error: 'bus down' });
});

test('Publish Skip Events off keeps the Skipped item and publishes nothing', async (t) => {
  const { skipped, rec } = await run(t, { operation: 'groomTicket', publishSkips: false }, [
    { json: envelope({ repo: '33god', board_id: OTHER_BOARD, ticket_key: 'GOD-1' }) },
  ]);
  assert.equal(rec.calls.length, 0);
  assert.equal(skipped[0].json.skipEvent.disabled, true);
});

test('delegate ignores a transition into any phase but the guarded one', async (t) => {
  const moved = envelope({ ...MOVED_TO_TODO.data, previous_phase: 'Todo', phase: 'In Progress' });
  const { skipped, rec } = await run(t, { operation: 'delegateTicket', phaseGuard: 'Todo,unstarted' }, [{ json: moved }]);
  assert.equal(rec.commands().length, 0);
  assert.equal(skipped[0].json.code, 'phase_guard');
  assert.match(skipped[0].json.reason, /phase is In Progress/);
  assert.equal(rec.skips()[0].data.skip_code, 'phase_guard');
  assert.equal(rec.skips()[0].data.context.reason, 'ticket-delegation');
});

test('the provider guard lets one shared trigger feed several operations', async (t) => {
  const { skipped, rec } = await run(t, { operation: 'groomTicket', providerEventGuard: 'plane.ticket.created' }, [
    { json: MOVED_TO_TODO },
  ]);
  assert.equal(rec.commands().length, 0);
  assert.equal(skipped[0].json.code, 'provider_event_guard');
  assert.match(skipped[0].json.reason, /provider_event_type is plane\.ticket\.transitioned/);
});

test('the provider guard accepts the multiOptions array form', async (t) => {
  const { dispatched } = await run(
    t,
    { operation: 'groomTicket', providerEventGuard: ['plane.ticket.created', 'plane.ticket.updated'] },
    [{ json: CREATED }],
  );
  assert.equal(dispatched.length, 1);
});

test('a configured provider guard is strict: no provider_event_type means skip', async (t) => {
  const { data } = CREATED;
  const { provider_event_type: _drop, ...bare } = data;
  const { skipped, rec } = await run(t, { operation: 'groomTicket', providerEventGuard: ['plane.ticket.created'] }, [
    { json: envelope(bare) },
  ]);
  assert.equal(rec.commands().length, 0);
  assert.equal(skipped[0].json.code, 'provider_event_guard');
  assert.match(skipped[0].json.reason, /provider_event_type is absent/);
});

test('an empty provider guard accepts an item with no provider_event_type', async (t) => {
  const { provider_event_type: _drop, ...bare } = CREATED.data;
  const { dispatched } = await run(t, { operation: 'groomTicket', providerEventGuard: [] }, [{ json: envelope(bare) }]);
  assert.equal(dispatched.length, 1);
});

test('the legacy On Ineligible = Error still fails the item', async (t) => {
  await assert.rejects(
    () => run(t, { operation: 'groomTicket', onIneligible: 'error' }, [
      { json: envelope({ repo: '33god', board_id: OTHER_BOARD, ticket_key: 'GOD-1' }) },
    ]),
    /bloodbank\.enabled is false/,
  );
});

test('an unowned board is a no_route skip with a null target', async (t) => {
  const { skipped, rec } = await run(t, { operation: 'groomTicket' }, [
    { json: envelope({ repo: 'nobody', board_id: 'ffffffff-0000-4000-8000-000000000000', ticket_key: 'NB-1' }) },
  ]);
  assert.equal(skipped[0].json.code, 'no_route');
  assert.equal(skipped[0].json.agentId, null);
  assert.equal(rec.skips()[0].data.target_agent_id, null);
});

test('continueOnFail routes an erroring item to Skipped', async (t) => {
  const { skipped } = await run(t, { operation: 'groomTicket' }, [
    { json: envelope({ ticket_key: 'JIMB-1' }) },
  ], LIVE_REGISTRY, { continueOnFail: true });
  assert.equal(skipped[0].json.code, 'error');
  assert.match(skipped[0].json.error, /neither data\.repo nor a board id/);
});

// ---------------------------------------------------------------------------
// Node: the Krebs execution fence
// ---------------------------------------------------------------------------

for (const mode of ['managed', 'shadow']) {
  test(`${mode} canonical manifest fences actual fleet publication`, async (t) => {
    const dir = await tempDir(t, '.fence-test-');
    await writeFile(join(dir, '.project.json'), JSON.stringify({ execution: { mode } }));
    const fenced = registry({ 'james-brennan-pm': agent({ project_path: dir }) });
    for (const operation of ['groomTicket', 'delegateTicket']) {
      const { skipped, rec } = await run(t, { operation }, [{ json: operation === 'groomTicket' ? CREATED : MOVED_TO_TODO }], fenced);
      assert.equal(rec.commands().length, 0);
      assert.equal(skipped[0].json.code, 'fenced');
      assert.match(skipped[0].json.reason, /Krebs/);
    }
  });
}

test('a missing .project.json (ENOENT) is legacy: dispatch proceeds', async (t) => {
  const dir = await tempDir(t, '.fence-test-');
  assert.equal(await executionMode(dir), 'legacy');
  const { dispatched } = await run(t, { operation: 'groomTicket' }, [{ json: CREATED }],
    registry({ 'james-brennan-pm': agent({ project_path: join(dir, 'no-such-repo') }) }));
  assert.equal(dispatched.length, 1);
});

test('an unreadable or invalid manifest is legacy, never a crash', async (t) => {
  const dir = await tempDir(t, '.fence-test-');
  await writeFile(join(dir, '.project.json'), '{ not json');
  assert.equal(await executionMode(dir), 'legacy');
  await writeFile(join(dir, '.project.json'), JSON.stringify({ execution: 'managed' }));
  assert.equal(await executionMode(dir), 'legacy');
  await writeFile(join(dir, '.project.json'), JSON.stringify({ execution: { mode: 'legacy' } }));
  assert.equal(await executionMode(dir), 'legacy');
  const { dispatched } = await run(t, { operation: 'groomTicket' }, [{ json: CREATED }],
    registry({ 'james-brennan-pm': agent({ project_path: dir }) }));
  assert.equal(dispatched.length, 1);
});

test('eligibility is judged before the fence: a switched-off managed project reports ineligible', async (t) => {
  const dir = await tempDir(t, '.fence-test-');
  await writeFile(join(dir, '.project.json'), JSON.stringify({ execution: { mode: 'managed' } }));
  const off = registry({
    'james-brennan-pm': agent({
      project_path: dir,
      bloodbank: { enabled: false, gateway_scope: 'fleet', target_agent_id: 'james-brennan-pm' },
    }),
  });
  const { skipped } = await run(t, { operation: 'groomTicket' }, [{ json: CREATED }], off);
  assert.equal(skipped[0].json.code, 'ineligible');
});
