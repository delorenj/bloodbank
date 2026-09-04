const assert = require('node:assert/strict');
const { mkdtemp, rm, writeFile } = require('node:fs/promises');
const { join } = require('node:path');
const test = require('node:test');
const { stringify: stringifyYaml } = require('yaml');

const {
  Fleet,
  delegationPrompt,
  groomingPrompt,
  resolveFleetAgentForBoard,
  ticketCorrelationId,
  ticketFactsFromEnvelope,
} = require('../src/index.ts');

const BOARD = 'a8a12be1-b3ab-44f4-ab24-abe8829aeb72';
const OTHER_BOARD = '15258893-0206-4e8f-aea6-340eb217988c';

function agent(overrides = {}) {
  return {
    repo: 'james-brennan',
    profile_name: 'james-brennan-pm',
    project_path: '/home/delorenj/code/james-brennan',
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

async function writeRegistry(t, value) {
  const dir = await mkdtemp(join(__dirname, '..', 'node_modules', '.fleet-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const file = join(dir, 'agents-registry.yaml');
  await writeFile(file, stringifyYaml(value), 'utf8');
  return file;
}

function executionContext(parameters, input) {
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
    continueOnFail: () => false,
  };
}

function recorder() {
  const calls = [];
  return {
    calls,
    async send(options) {
      calls.push(options);
      return {
        subject: 'bloodbank.cmd.agent.invocation.start',
        correlationid: options.correlationId,
        eventId: 'event-id',
        commandId: 'command-id',
      };
    },
  };
}

test('the board id resolves the owning agent, not the repo slug', () => {
  const route = resolveFleetAgentForBoard(LIVE_REGISTRY, BOARD, '');
  assert.equal(route.agentId, 'james-brennan-pm');
  assert.equal(route.eligible, true);
  assert.equal(route.matchedBy, 'board');
  assert.equal(route.projectPath, '/home/delorenj/code/james-brennan');
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
  assert.match(route.why, /bloodbank\.enabled is not true/);
  assert.equal(route.agentId, '33god-pm');
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

test('an unknown board with no fallback is ineligible rather than an error', () => {
  const route = resolveFleetAgentForBoard(registry({}), BOARD, 'nobody');
  assert.equal(route.eligible, false);
  assert.match(route.why, /no registry entry matches board/);
});

test('a ticket correlation id is byte-identical to the Python publisher it replaces', () => {
  // Recorded from the live bb-triage-invoke run that groomed JIMB-273. The
  // port is only safe if a replayed webhook lands on the same thread.
  assert.equal(ticketCorrelationId(BOARD, 'JIMB-273'), 'dda43316-59d5-52d8-a66d-4295c9ed97fe');
});

test('correlation is per ticket, so two tickets are two conversations', () => {
  assert.notEqual(ticketCorrelationId(BOARD, 'JIMB-273'), ticketCorrelationId(BOARD, 'JIMB-274'));
  assert.equal(ticketCorrelationId(BOARD, 'JIMB-273'), ticketCorrelationId(BOARD, 'JIMB-273'));
});

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
  // The board id survives an unknown checkout path: it is the fact that tells a
  // multi-tenant Plane which workspace to answer as, so it is never optional.
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

test('groom publishes one invocation command addressed to the resolved agent', async () => {
  const registryFile = await writeRegistry(t_ctx(), LIVE_REGISTRY);
  const { calls, send } = recorder();
  const context = executionContext(
    { operation: 'groomTicket', registryFile, providerEventGuard: 'plane.ticket.created' },
    [{ json: CREATED }],
  );
  const [out] = await Fleet.prototype.execute.call(context, send);

  assert.equal(calls.length, 1);
  assert.equal(calls[0].type, 'bloodbank.agent.invocation.start');
  assert.equal(calls[0].kind, 'command');
  assert.equal(calls[0].data.target_agent_id, 'james-brennan-pm');
  assert.equal(calls[0].correlationId, 'dda43316-59d5-52d8-a66d-4295c9ed97fe');
  assert.equal(calls[0].causationId, CREATED.id);
  assert.equal(calls[0].data.context.reason, 'ticket-grooming');
  assert.equal(calls[0].data.context.ticket_key, 'JIMB-273');
  assert.equal(out[0].json.invoked, true);
  assert.equal(out[0].json.agentId, 'james-brennan-pm');
});

test('an ineligible project is a green skip, not a failed execution', async () => {
  const registryFile = await writeRegistry(t_ctx(), LIVE_REGISTRY);
  const { calls, send } = recorder();
  const context = executionContext({ operation: 'groomTicket', registryFile }, [
    { json: envelope({ repo: '33god', board_id: OTHER_BOARD, ticket_key: 'GOD-1', provider_event_type: 'plane.ticket.created' }) },
  ]);
  const [out] = await Fleet.prototype.execute.call(context, send);

  assert.equal(calls.length, 0);
  assert.equal(out[0].json.invoked, false);
  assert.equal(out[0].json.skipped, true);
  assert.match(out[0].json.reason, /bloodbank\.enabled is not true/);
});

test('delegate ignores a transition into any phase but the guarded one', async () => {
  const registryFile = await writeRegistry(t_ctx(), LIVE_REGISTRY);
  const { calls, send } = recorder();
  const moved = envelope({
    ...MOVED_TO_TODO.data,
    previous_phase: 'Todo',
    phase: 'In Progress',
  });
  const context = executionContext(
    { operation: 'delegateTicket', registryFile, phaseGuard: 'Todo,unstarted' },
    [{ json: moved }],
  );
  const [out] = await Fleet.prototype.execute.call(context, send);

  assert.equal(calls.length, 0);
  assert.equal(out[0].json.skipped, true);
  assert.match(out[0].json.reason, /phase is In Progress/);
});

test('delegate fires on a transition into Todo', async () => {
  const registryFile = await writeRegistry(t_ctx(), LIVE_REGISTRY);
  const { calls, send } = recorder();
  const context = executionContext(
    { operation: 'delegateTicket', registryFile, phaseGuard: 'Todo,unstarted' },
    [{ json: MOVED_TO_TODO }],
  );
  const [out] = await Fleet.prototype.execute.call(context, send);

  assert.equal(calls.length, 1);
  assert.equal(calls[0].data.context.reason, 'ticket-delegation');
  assert.equal(calls[0].data.context.previous_phase, 'Backlog');
  assert.match(calls[0].data.prompt, /has moved into Todo/);
  assert.equal(out[0].json.invoked, true);
});

test('the provider guard lets one shared trigger feed several operations', async () => {
  const registryFile = await writeRegistry(t_ctx(), LIVE_REGISTRY);
  const { calls, send } = recorder();
  const context = executionContext(
    { operation: 'groomTicket', registryFile, providerEventGuard: 'plane.ticket.created' },
    [{ json: MOVED_TO_TODO }],
  );
  const [out] = await Fleet.prototype.execute.call(context, send);

  assert.equal(calls.length, 0);
  assert.match(out[0].json.reason, /provider_event_type is plane\.ticket\.transitioned/);
});

test('an inherited correlation id wins, so a caused command stays on its thread', async () => {
  const registryFile = await writeRegistry(t_ctx(), LIVE_REGISTRY);
  const { calls, send } = recorder();
  const inherited = '9f9f9f9f-1111-4222-8333-444444444444';
  const context = executionContext({ operation: 'groomTicket', registryFile }, [
    { json: envelope(CREATED.data, { correlationid: inherited }) },
  ]);
  await Fleet.prototype.execute.call(context, send);
  assert.equal(calls[0].correlationId, inherited);
});

test('an item with neither repo nor board id is a hard error, not a silent skip', async () => {
  const registryFile = await writeRegistry(t_ctx(), LIVE_REGISTRY);
  const { send } = recorder();
  const context = executionContext({ operation: 'groomTicket', registryFile }, [
    { json: envelope({ ticket_key: 'JIMB-1' }) },
  ]);
  await assert.rejects(
    () => Fleet.prototype.execute.call(context, send),
    /neither data\.repo nor a board id/,
  );
});

test('invoke sends the caller prompt verbatim', async () => {
  const registryFile = await writeRegistry(t_ctx(), LIVE_REGISTRY);
  const { calls, send } = recorder();
  const context = executionContext(
    { operation: 'invoke', registryFile, prompt: 'Report the current cycle.' },
    [{ json: CREATED }],
  );
  await Fleet.prototype.execute.call(context, send);
  assert.equal(calls[0].data.prompt, 'Report the current cycle.');
  assert.equal(calls[0].data.context.reason, 'fleet-invoke');
});

test('ticket overrides win over the incoming envelope', async () => {
  const registryFile = await writeRegistry(t_ctx(), LIVE_REGISTRY);
  const { calls, send } = recorder();
  const context = executionContext(
    {
      operation: 'groomTicket',
      registryFile,
      ticket: { boardId: BOARD, repo: 'james-brennan', ticketKey: 'JIMB-999' },
    },
    [{ json: { unrelated: true } }],
  );
  await Fleet.prototype.execute.call(context, send);
  assert.equal(calls[0].data.context.ticket_key, 'JIMB-999');
  assert.equal(calls[0].correlationId, ticketCorrelationId(BOARD, 'JIMB-999'));
});

// node:test does not hand `t` to the module scope, and writeRegistry only needs
// somewhere to register cleanup. A per-file shim keeps the helper signature
// honest without threading `t` through every case.
const cleanups = [];
function t_ctx() {
  return { after: (fn) => cleanups.push(fn) };
}
test.after(async () => {
  for (const fn of cleanups) await fn();
});
