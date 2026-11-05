// "Ticket Delegation — Groomed in Todo", run as it is exported from the live n8n.
//
// The workflow lives in n8n; its export is synced every 15 minutes into
// delonet-n8n-workflows. This test reads that export (never a hand copy, which
// is what drifted before) and drives the lane end to end with no network: the
// trigger's own filter, the two Code nodes in a VM, a faked Plane read, and the
// real Fleet node with the workflow's own parameters. The input is the shape of
// the grooming completion that HEYMA-10 produced and nobody ever delegated
// (event ea566d8a-7934-5f59-afc9-2f645554642f, Candystore, 2026-10-18).
//
// Set N8N_WORKFLOWS_DIR to point at another checkout of the export; without one
// the tests skip and say why.
const assert = require('node:assert/strict');
const { existsSync, readdirSync, readFileSync } = require('node:fs');
const { mkdtemp, rm, writeFile } = require('node:fs/promises');
const { homedir } = require('node:os');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { stringify: stringifyYaml } = require('yaml');

const {
  Fleet,
  decideTriggerMessage,
  durableConsumerName,
  fleetCommandId,
  parseDataConditions,
  publish,
  triggerAccepts,
  validateEnvelope,
} = require('../src/index.ts');

const NAME = 'Ticket Delegation — Groomed in Todo';
const COMMAND = 'bloodbank.agent.invocation.start';
const SKIPPED = 'bloodbank.agent.invocation.skipped';
const HEYMA_BOARD = '7485039f-85b0-47db-ad99-d11497f043d7';
const HEYMA_TICKET = '2a099b89-5612-4488-b7ca-80b196d5684d';

function locateExport() {
  const dir = process.env.N8N_WORKFLOWS_DIR || join(homedir(), 'code', 'delonet-n8n-workflows', 'workflows');
  if (!existsSync(dir)) return { reason: `no workflow export directory at ${dir}` };
  let draft;
  let published;
  for (const file of readdirSync(dir)) {
    if (!file.endsWith('.json')) continue;
    let workflow;
    try {
      workflow = JSON.parse(readFileSync(join(dir, file), 'utf8'));
    } catch {
      continue;
    }
    if (workflow.name !== NAME) continue;
    if (file.endsWith('.published.json')) published = workflow;
    else draft = workflow;
  }
  // The published version is what is actually running.
  const workflow = published || draft;
  return workflow ? { workflow } : { reason: `no export of "${NAME}" in ${dir}` };
}

const located = locateExport();
const WORKFLOW = located.workflow;
const skip = WORKFLOW ? false : located.reason;

const node = (name) => {
  const found = WORKFLOW.nodes.find((candidate) => candidate.name === name);
  assert.ok(found, `the workflow has a "${name}" node`);
  return found;
};

/** HEYMA-10's grooming completion as the gateway published it (Candystore row
 *  ea566d8a-7934-5f59-afc9-2f645554642f, with the wire's specversion/kind). */
const HEYMA_10_COMPLETED = Object.freeze({
  specversion: '1.0',
  id: 'ea566d8a-7934-5f59-afc9-2f645554642f',
  type: 'bloodbank.agent.invocation.completed',
  kind: 'event',
  time: '2026-10-18T23:07:18.883498Z',
  producer: 'hermes-agent:heyma-pm',
  service: 'bloodbank-hermes-gateway',
  domain: 'agent',
  correlationid: 'ae662a13-823a-51ea-9061-ade63bfe79e5',
  actor: { cli: 'hermes', type: 'agent_api', model: null, agent_id: 'heyma-pm', provider: null },
  data: {
    lane: 'batch',
    context: {
      repo: 'transcription-queue',
      phase: 'Todo',
      title: 'Recover 3 Wax items stuck at diarization (CUDA OOM): bounded owner retry of bc81421c7b194b2b, e5a727403046d70c, ab8f6337e387119c',
      reason: 'ticket-grooming',
      board_id: HEYMA_BOARD,
      ticket_id: HEYMA_TICKET,
      workspace: '33god',
      ticket_key: 'HEYMA-10',
      previous_phase: '',
      provider_event_type: 'plane.ticket.created',
    },
    turn_id: '97846b24-b253-53de-87f0-7be928a5d6a1',
    thread_id: 'bloodbank:ae662a13-823a-51ea-9061-ade63bfe79e5',
    command_id: '97846b24-b253-53de-87f0-7be928a5d6a1',
    profile_name: 'heyma-pm',
    invocation_id: '97846b24-b253-53de-87f0-7be928a5d6a1',
    idempotency_key: 'agent.invocation.start:target:heyma-pm:command:97846b24-b253-53de-87f0-7be928a5d6a1',
    target_agent_id: 'heyma-pm',
  },
});

function completion(overrides = {}, context = {}) {
  const base = JSON.parse(JSON.stringify(HEYMA_10_COMPLETED));
  return { ...base, ...overrides, data: { ...base.data, context: { ...base.data.context, ...context } } };
}

/** A Plane v1 issue read with ?expand=state,labels. */
function planeIssue({ state = 'Todo', group = 'unstarted', labels = ['lifecycle:triaged', 'bug'], ...extra } = {}) {
  return {
    id: HEYMA_TICKET,
    sequence_id: 10,
    name: HEYMA_10_COMPLETED.data.context.title,
    deleted_at: null,
    archived_at: null,
    state: { id: 'f02ca3e5-62b3-4788-bfd4-1d1536408dad', name: state, color: '#000000', group },
    labels: labels.map((name, index) => ({ id: `0000000${index}-0000-4000-8000-000000000000`, name })),
    assignees: [],
    ...extra,
  };
}

const HEYMA_REGISTRY = {
  schema_version: 1,
  agents: {
    'heyma-pm': {
      repo: 'transcription-queue',
      profile_name: 'heyma-pm',
      project_path: '/nonexistent/HeyMa',
      plane: { identifier: 'HEYMA', project_id: HEYMA_BOARD, workspace: '33god' },
      bloodbank: { enabled: true, gateway_scope: 'fleet', target_agent_id: 'heyma-pm' },
    },
  },
};

/** Run one n8n Code node (runOnceForAllItems) in a VM, as n8n would. */
function runCode(codeNode, input, references = {}) {
  const sandbox = {
    $input: { all: () => input, first: () => input[0] },
    $: (name) => {
      const items = references[name];
      if (!items) throw new Error(`Code node referenced unknown node "${name}"`);
      return { all: () => items, first: () => items[0], itemMatching: (index) => items[index] };
    },
    $env: {},
  };
  const script = new vm.Script(`(function () {\n${codeNode.parameters.jsCode}\n})()`);
  // Out of the VM's realm, so deepEqual compares plain values.
  return JSON.parse(JSON.stringify(script.runInNewContext(sandbox)));
}

/** n8n's `={{ expr }}` for the whole-value expressions this workflow uses. */
function resolveParameter(value, json) {
  if (typeof value !== 'string' || !value.startsWith('={{')) return value;
  const expression = value.trim();
  assert.ok(expression.endsWith('}}'), `whole-value expression: ${value}`);
  // eslint-disable-next-line no-new-func
  return new Function('$json', `return (${expression.slice(3, -2)});`)(json);
}

async function writeRegistry(t, value) {
  const dir = await mkdtemp(join(__dirname, '..', 'node_modules', '.groomed-lane-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const file = join(dir, 'agents-registry.yaml');
  await writeFile(file, stringifyYaml(value), 'utf8');
  return file;
}

function recorder() {
  const calls = [];
  return {
    calls,
    commands: () => calls.filter((call) => call.type === COMMAND),
    skips: () => calls.filter((call) => call.type === SKIPPED),
    async send(options) {
      calls.push(options);
      return {
        subject: options.type === COMMAND ? 'bloodbank.cmd.agent.invocation.start' : 'bloodbank.evt.agent.invocation.skipped',
        correlationid: options.correlationId,
        eventId: options.eventId || 'event-id',
        commandId: options.commandId,
      };
    },
  };
}

/** Delegate Ticket with the workflow's own parameters, expressions resolved per item. */
async function runDelegate(t, items, { registry = HEYMA_REGISTRY, send } = {}) {
  const registryFile = await writeRegistry(t, registry);
  const parameters = node('Delegate Ticket').parameters;
  const rec = recorder();
  const context = {
    getInputData: () => items,
    getNodeParameter(name, index, fallback) {
      if (name === 'registryFile') return registryFile;
      if (!Object.prototype.hasOwnProperty.call(parameters, name)) return fallback;
      return resolveParameter(parameters[name], items[index].json);
    },
    getNode: () => node('Delegate Ticket'),
    continueOnFail: () => false,
  };
  const [dispatched, skipped] = await Fleet.prototype.execute.call(context, send || rec.send);
  return { dispatched, skipped, rec };
}

/** The lane up to the Fleet node: Target -> (Plane read) -> Groomed in Todo?. */
function gate(envelope, planeAnswer) {
  const targets = runCode(node('Groomed — Target'), [{ json: envelope }]);
  if (!targets.length) return { targets, gated: [] };
  const answers = targets.map(() => ({ json: planeAnswer }));
  return { targets, gated: runCode(node('Groomed in Todo?'), answers, { 'Groomed — Target': targets }) };
}

function triggerParts() {
  const trigger = node('onGroomingCompleted');
  return {
    trigger,
    bindings: trigger.parameters.events,
    conditions: parseDataConditions(trigger.parameters.dataMatch),
  };
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

test('the lane is wired Trigger -> Target -> Read Ticket -> gate -> Delegate -> ntfy, with no clock', { skip }, () => {
  const next = (from, output = 0) => (WORKFLOW.connections[from]?.main?.[output] || []).map((edge) => edge.node);
  assert.deepEqual(next('onGroomingCompleted'), ['Groomed — Target']);
  assert.deepEqual(next('Groomed — Target'), ['Read Ticket']);
  assert.deepEqual(next('Read Ticket'), ['Groomed in Todo?']);
  assert.deepEqual(next('Groomed in Todo?'), ['Delegate Ticket']);
  assert.deepEqual(next('Delegate Ticket', 0), ['Delegation Started']);
  assert.deepEqual(next('Delegate Ticket', 1), ['Notable Skip?']);
  assert.deepEqual(next('Notable Skip?'), ['Delegation Skipped']);
  // Candybar is the single clock: no schedule, no poller.
  assert.ok(!WORKFLOW.nodes.some((candidate) => /schedule|cron|interval/i.test(candidate.type)));
  const triggers = WORKFLOW.nodes.filter((candidate) => /trigger/i.test(candidate.type));
  assert.deepEqual(triggers.map((candidate) => candidate.name), ['onGroomingCompleted']);
});

test('Read Ticket reads the live issue with state and label names, never failing the execution itself', { skip }, () => {
  const read = node('Read Ticket');
  assert.equal(read.type, 'n8n-nodes-base.httpRequest');
  assert.equal(read.onError, 'continueRegularOutput');
  assert.equal(read.credentials.httpHeaderAuth.id, 'TpxVVgOnmjHaE6h7');
  assert.equal((read.parameters.method || 'GET').toUpperCase(), 'GET');
  const target = { ws: '33god', board: HEYMA_BOARD, ticketId: HEYMA_TICKET };
  const url = read.parameters.url
    .replace(/^=/, '')
    .replace(/\{\{\s*\$json\.(\w+)\s*\}\}/g, (_match, key) => target[key]);
  assert.equal(
    url,
    `https://plane.delo.sh/api/v1/workspaces/33god/projects/${HEYMA_BOARD}/issues/${HEYMA_TICKET}/?expand=state,labels`,
  );
});

test('Delegate Ticket mirrors the transition lane, mapped from the completion', { skip }, () => {
  const delegate = node('Delegate Ticket');
  const p = delegate.parameters;
  assert.equal(delegate.type, 'n8n-nodes-bloodbank.bloodbankFleet');
  assert.equal(p.operation, 'delegateTicket');
  assert.equal(p.phaseGuard, 'Todo');
  // The completion is not a provider event and the guard is strict: it must be empty.
  assert.deepEqual(p.providerEventGuard ?? [], []);
  assert.equal(p.requiredLabel, 'lifecycle:triaged');
  assert.equal(p.service, 'n8n-ticket-delegation-groomed');
  assert.equal(p.publishSkips, true);
  assert.equal(p.causationId, '={{ $json.id }}');
  assert.equal(p.correlationId, '={{ $json.correlationid }}');
  for (const name of ['Delegation Started', 'Delegation Skipped']) {
    assert.equal(node(name).onError, 'continueRegularOutput');
    assert.equal(node(name).parameters.topic, 'lifecycle');
  }
});

// ---------------------------------------------------------------------------
// Trigger: what can start the lane
// ---------------------------------------------------------------------------

test('the trigger takes HEYMA-10\'s grooming completion and nothing near it', { skip }, () => {
  const { trigger, bindings, conditions } = triggerParts();
  assert.deepEqual(bindings, ['bloodbank.agent.invocation.completed']);
  assert.equal(trigger.parameters.delivery ?? 'durable', 'durable');
  assert.equal(trigger.parameters.acknowledge ?? 'afterExecution', 'afterExecution');
  const accepts = (envelope) => triggerAccepts('event', bindings, conditions, envelope);

  assert.equal(accepts(HEYMA_10_COMPLETED), true);
  const wire = Buffer.from(JSON.stringify(HEYMA_10_COMPLETED));
  assert.equal(decideTriggerMessage('event', bindings, conditions, wire).verdict, 'emit');

  // A delegation turn's own completion: the lane cannot trigger itself.
  assert.equal(accepts(completion({}, { reason: 'ticket-delegation' })), false);
  // Created in Backlog: the transition lane's, when a person promotes it.
  assert.equal(accepts(completion({}, { phase: 'Backlog' })), false);
  // A failed grooming turn, a start, an untagged turn.
  assert.equal(accepts(completion({ type: 'bloodbank.agent.invocation.failed' })), false);
  assert.equal(accepts(completion({ type: 'bloodbank.agent.invocation.started' })), false);
  const untagged = completion();
  delete untagged.data.context.reason;
  assert.equal(accepts(untagged), false);
});

test('the trigger\'s durable is its own, never the Pickup Chip\'s', { skip }, () => {
  const { trigger } = triggerParts();
  const own = durableConsumerName(WORKFLOW.id, trigger.id);
  assert.equal(own, `n8n-${WORKFLOW.id}-${trigger.id}`);
  // The chip also consumes invocation.completed, on its own durable.
  assert.notEqual(own, durableConsumerName('wWXgCZiiIBWaRRzE', '1a4f5ee5-dd90-5b25-80bb-dc48568f35f0'));
});

// ---------------------------------------------------------------------------
// The live gate
// ---------------------------------------------------------------------------

test('a ticket still in Todo with lifecycle:triaged goes on as a delegation envelope', { skip }, () => {
  const { gated } = gate(HEYMA_10_COMPLETED, planeIssue());
  assert.equal(gated.length, 1);
  const item = gated[0].json;
  assert.equal(item.id, HEYMA_10_COMPLETED.id);
  assert.equal(item.time, HEYMA_10_COMPLETED.time);
  assert.equal(item.correlationid, HEYMA_10_COMPLETED.correlationid);
  assert.equal(item.data.phase, 'Todo');
  assert.equal(item.data.ticket_key, 'HEYMA-10');
  assert.equal(item.data.board_id, HEYMA_BOARD);
  assert.equal(item.data.provider_event_type, 'plane.ticket.created');
  assert.deepEqual(item.data.labels, ['lifecycle:triaged', 'bug']);
});

test('the live ticket decides, not the echoed phase', { skip }, () => {
  const stops = [
    ['HEYMA-10 today: Done', planeIssue({ state: 'Done', group: 'completed' })],
    ['claimed mid-grooming (PJAN-202)', planeIssue({ state: 'In Progress', group: 'started' })],
    ['a state group is not a state name', planeIssue({ state: 'unstarted' })],
    ['the name must be exactly Todo', planeIssue({ state: 'todo' })],
    ['grooming stopped to ask a question', planeIssue({ labels: ['bug'] })],
    ['no labels at all', planeIssue({ labels: [] })],
    ['deleted since', planeIssue({ deleted_at: '2026-10-19T00:00:00Z' })],
    ['archived since', planeIssue({ archived_at: '2026-10-19T00:00:00Z' })],
  ];
  for (const [why, issue] of stops) {
    assert.deepEqual(gate(HEYMA_10_COMPLETED, issue).gated, [], why);
  }
  // Label names compare like the Fleet node's: trimmed, case-insensitive.
  assert.equal(gate(HEYMA_10_COMPLETED, planeIssue({ labels: [' Lifecycle:Triaged '] })).gated.length, 1);
});

test('a deleted ticket is dropped green; any other Plane failure fails loudly', { skip }, () => {
  assert.deepEqual(gate(HEYMA_10_COMPLETED, { error: { message: '404 - Not Found', httpCode: '404' } }).gated, []);
  assert.deepEqual(gate(HEYMA_10_COMPLETED, { error: { message: 'Gone', status: 410 } }).gated, []);
  assert.throws(
    () => gate(HEYMA_10_COMPLETED, { error: { message: '401 - Unauthorized', httpCode: '401' } }),
    /Read Ticket failed for HEYMA-10 .*HTTP 401/,
  );
  // An unexpanded answer would silently drop every ticket; it must fail instead.
  assert.throws(
    () => gate(HEYMA_10_COMPLETED, { ...planeIssue(), state: 'f02ca3e5-62b3-4788-bfd4-1d1536408dad' }),
    /expand=state,labels/,
  );
  assert.throws(
    () => gate(HEYMA_10_COMPLETED, { ...planeIssue(), labels: ['8a4da0a9-35c3-4816-9cc3-a9f163160976'] }),
    /expand=state,labels/,
  );
});

test('a completion that names no ticket never reaches Plane', { skip }, () => {
  for (const missing of ['workspace', 'board_id', 'ticket_id']) {
    const envelope = completion();
    delete envelope.data.context[missing];
    assert.deepEqual(gate(envelope, planeIssue()).targets, [], missing);
  }
});

// ---------------------------------------------------------------------------
// End to end through the real Fleet node
// ---------------------------------------------------------------------------

test('HEYMA-10, groomed in Todo, becomes one delegation command caused by its completion', { skip }, async (t) => {
  const { gated } = gate(HEYMA_10_COMPLETED, planeIssue());
  const { dispatched, skipped, rec } = await runDelegate(t, gated);
  assert.equal(skipped.length, 0, JSON.stringify(skipped));
  assert.equal(dispatched.length, 1);
  const [command] = rec.commands();
  assert.equal(command.data.target_agent_id, 'heyma-pm');
  assert.equal(command.data.context.reason, 'ticket-delegation');
  assert.equal(command.data.context.ticket_key, 'HEYMA-10');
  assert.equal(command.data.context.ticket_id, HEYMA_TICKET);
  assert.equal(command.data.context.board_id, HEYMA_BOARD);
  assert.equal(command.data.context.workspace, '33god');
  assert.equal(command.data.context.repo, 'transcription-queue');
  assert.equal(command.data.context.phase, 'Todo');
  assert.equal(command.data.context.provider_event_type, 'plane.ticket.created');
  assert.equal(command.causationId, HEYMA_10_COMPLETED.id);
  assert.equal(command.correlationId, HEYMA_10_COMPLETED.correlationid);
  assert.equal(command.observedAt, HEYMA_10_COMPLETED.time);
  assert.equal(command.service, 'n8n-ticket-delegation-groomed');
  assert.equal(command.commandId, fleetCommandId(HEYMA_10_COMPLETED.id, 'delegateTicket', 'heyma-pm'));
  assert.match(command.data.prompt, /re-read the ticket live from Plane/);
  assert.match(command.data.prompt, /lifecycle:triaged/);
  assert.equal(dispatched[0].json.ticketKey, 'HEYMA-10');
  assert.equal(dispatched[0].json.agentId, 'heyma-pm');
});

test('the delegation turn\'s own completion cannot start the lane again', { skip }, async (t) => {
  const { gated } = gate(HEYMA_10_COMPLETED, planeIssue());
  const { rec } = await runDelegate(t, gated);
  const [command] = rec.commands();
  // The gateway echoes the command's data.context on the turn's completion.
  const echo = completion({ id: command.commandId }, command.data.context);
  const { bindings, conditions } = triggerParts();
  assert.equal(echo.data.context.phase, 'Todo');
  assert.equal(triggerAccepts('event', bindings, conditions, echo), false);
});

test('a redelivered completion rebuilds a byte-identical, schema-valid command', { skip }, async (t) => {
  const messages = [];
  const send = (options) => publish(options, async () => ({
    publish(subject, data) {
      messages.push({ subject, envelope: JSON.parse(Buffer.from(data).toString('utf8')) });
    },
    flush: async () => {},
    drain: async () => {},
  }));
  for (let attempt = 0; attempt < 2; attempt++) {
    await runDelegate(t, gate(HEYMA_10_COMPLETED, planeIssue()).gated, { send });
  }
  const commands = messages.filter((message) => message.envelope.type === COMMAND);
  assert.equal(commands.length, 2);
  assert.deepEqual(commands[0].envelope, commands[1].envelope);
  assert.match(commands[0].envelope.idempotency_key, /^agent\.invocation\.start:target:heyma-pm:command:/);
  assert.equal(commands[0].envelope.causationid, HEYMA_10_COMPLETED.id);
  assert.doesNotThrow(() => validateEnvelope(COMMAND, commands[0].envelope));
});

test('on a shared board the live labels pick the owning agent', { skip }, async (t) => {
  const shared = {
    schema_version: 1,
    agents: {
      'alpha-pm': {
        profile_name: 'alpha-pm',
        project_path: '/nonexistent/alpha',
        plane: { project_id: HEYMA_BOARD, workspace: '33god', scope_label: 'repo:alpha', scope_default: true },
        bloodbank: { enabled: true, gateway_scope: 'fleet', target_agent_id: 'alpha-pm' },
      },
      'beta-pm': {
        profile_name: 'beta-pm',
        project_path: '/nonexistent/beta',
        plane: { project_id: HEYMA_BOARD, workspace: '33god', scope_label: 'repo:beta' },
        bloodbank: { enabled: true, gateway_scope: 'fleet', target_agent_id: 'beta-pm' },
      },
    },
  };
  // The completion carries no labels; only the live read can route this one.
  const { gated } = gate(HEYMA_10_COMPLETED, planeIssue({ labels: ['lifecycle:triaged', 'repo:beta'] }));
  const { rec } = await runDelegate(t, gated, { registry: shared });
  assert.equal(rec.commands()[0].data.target_agent_id, 'beta-pm');
});

test('an ineligible owner is a published, paged skip', { skip }, async (t) => {
  const off = JSON.parse(JSON.stringify(HEYMA_REGISTRY));
  off.agents['heyma-pm'].bloodbank.enabled = false;
  const { dispatched, skipped, rec } = await runDelegate(t, gate(HEYMA_10_COMPLETED, planeIssue()).gated, { registry: off });
  assert.equal(dispatched.length, 0);
  assert.equal(skipped[0].json.code, 'ineligible');
  assert.equal(rec.skips().length, 1);
  assert.equal(rec.skips()[0].data.context.reason, 'ticket-delegation');
  // Notable Skip? passes everything but the two guard codes.
  const rules = node('Notable Skip?').parameters.conditions.conditions.map((rule) => rule.rightValue).sort();
  assert.deepEqual(rules, ['phase_guard', 'provider_event_guard']);
});

// ---------------------------------------------------------------------------
// Failure is paged: the lane's error workflow
// ---------------------------------------------------------------------------

// The trigger acks after the execution, so a failed run is never redelivered.
// HEYMA-12 (2026-11-05, execution 632616) died on a Plane 429 and left no trace
// but the n8n execution. The lane names an error workflow that pages ntfy
// `lifecycle` with the ticket and the requeue command.
const ERRORS_NAME = 'Lifecycle Lane Errors';

function locateByName(name) {
  const dir = process.env.N8N_WORKFLOWS_DIR || join(homedir(), 'code', 'delonet-n8n-workflows', 'workflows');
  if (!existsSync(dir)) return null;
  let draft;
  let published;
  for (const file of readdirSync(dir)) {
    if (!file.endsWith('.json')) continue;
    let workflow;
    try {
      workflow = JSON.parse(readFileSync(join(dir, file), 'utf8'));
    } catch {
      continue;
    }
    if (workflow.name !== name) continue;
    if (file.endsWith('.published.json')) published = workflow;
    else draft = workflow;
  }
  return published || draft || null;
}

const ERRORS = WORKFLOW ? locateByName(ERRORS_NAME) : null;
const errorsSkip = skip || (ERRORS ? false : `no export of "${ERRORS_NAME}"`);
const errorsNode = (name) => {
  const found = ERRORS.nodes.find((candidate) => candidate.name === name);
  assert.ok(found, `the error workflow has a "${name}" node`);
  return found;
};

// The error workflow describes the failure in one of two ways. Until 0.8.0
// is deployed and the live export rewired it is the Code node "Describe
// Failure"; afterwards it is the Fleet node's Report Lane Failure operation,
// which also publishes bloodbank.workflow.task.failed. These tests assert the
// wiring that exists, whichever it is.
const reportNode = () =>
  ERRORS.nodes.find(
    (candidate) =>
      candidate.type === 'n8n-nodes-bloodbank.bloodbankFleet' &&
      candidate.parameters?.operation === 'reportLaneFailure',
  );
const describeCode = () => ERRORS.nodes.find((candidate) => candidate.name === 'Describe Failure');
const useCode = () => Boolean(describeCode()) && !reportNode();
const codeOnly = () => (errorsSkip ? errorsSkip : useCode() ? false : 'the live error workflow already reports through the Fleet node');

test('the lane names Lifecycle Lane Errors as its error workflow, and it is active', { skip: errorsSkip }, () => {
  assert.equal(WORKFLOW.settings?.errorWorkflow, ERRORS.id);
  // n8n refuses to run an inactive error workflow ("is not active and cannot be executed").
  assert.equal(ERRORS.active, true);
  const next = (from, output = 0) => (ERRORS.connections[from]?.main?.[output] || []).map((edge) => edge.node);
  assert.equal(errorsNode('Lane Failed').type, 'n8n-nodes-base.errorTrigger');
  assert.deepEqual(next('Lane Failed'), ['Execution Lookup']);
  if (useCode()) {
    assert.deepEqual(next('Execution Lookup'), ['Describe Failure']);
    assert.deepEqual(next('Describe Failure'), ['Page Lifecycle']);
  } else {
    const report = reportNode();
    assert.ok(report, 'the error workflow has either Describe Failure or a Fleet Report Lane Failure node');
    assert.deepEqual(next('Execution Lookup'), [report.name]);
    // Published and Not Published both page: the page is the point.
    assert.ok(next(report.name, 0).includes('Page Lifecycle'));
    assert.ok(next(report.name, 1).includes('Page Lifecycle'));
  }
  // A failed lookup still pages, just without the ticket.
  assert.equal(errorsNode('Execution Lookup').onError, 'continueRegularOutput');
  assert.equal(errorsNode('Page Lifecycle').parameters.topic, 'lifecycle');
  assert.ok(!ERRORS.nodes.some((candidate) => /schedule|cron|interval/i.test(candidate.type)));
});

const HEYMA_12_ERROR = Object.freeze({
  execution: {
    id: '632616',
    url: 'https://n8n.delo.sh/workflow/l0J53w0TsS5PwETk/executions/632616',
    lastNodeExecuted: 'Groomed in Todo?',
    error: { message: 'Plane read failed: HTTP 429' },
  },
  workflow: { id: 'l0J53w0TsS5PwETk', name: NAME },
});

test('a failed lane run pages the ticket it dropped and how to requeue it', { skip: codeOnly() }, () => {
  // The failed execution's own data, as the n8n API returns it with includeData.
  const lookup = {
    id: '632616',
    status: 'error',
    data: { resultData: { runData: {
      onGroomingCompleted: [{ data: { main: [[{ json: { data: { context: { ticket_key: 'HEYMA-12', workspace: '33god' } } } }]] } }],
      'Groomed — Target': [{ data: { main: [[{ json: { ws: '33god', ticketKey: 'HEYMA-12', ticketId: HEYMA_TICKET } }]] } }],
    } } },
  };
  const [page] = runCode(errorsNode('Describe Failure'), [{ json: lookup }], { 'Lane Failed': [{ json: HEYMA_12_ERROR }] });
  assert.equal(page.json.title, 'Lane failed: HEYMA-12');
  assert.equal(page.json.ticketKey, 'HEYMA-12');
  assert.match(page.json.message, /failed at Groomed in Todo\?: Plane read failed: HTTP 429/);
  assert.match(page.json.message, /px move HEYMA-12 Backlog -m "lane failed \(execution 632616\)" && px move HEYMA-12 Todo/);
  assert.equal(page.json.click, HEYMA_12_ERROR.execution.url);
});

test('a lookup that failed still pages, without a ticket', { skip: codeOnly() }, () => {
  const [page] = runCode(errorsNode('Describe Failure'), [{ json: { error: { message: 'ECONNREFUSED' } } }], {
    'Lane Failed': [{ json: HEYMA_12_ERROR }],
  });
  assert.equal(page.json.ticketKey, null);
  assert.equal(page.json.lookupOk, false);
  assert.equal(page.json.title, `Lane failed: ${NAME}`);
  assert.match(page.json.message, /Ticket unknown/);
  assert.doesNotMatch(page.json.message, /px move/);
});

// The Fleet node's own behaviour is covered by lane-failure.test.cjs; here we
// only check that the live export, once rewired, hands it the right inputs.
test('when the error workflow reports through the Fleet node it is wired for the contract', { skip: errorsSkip || (ERRORS && reportNode() ? false : 'the live error workflow still uses Describe Failure') }, () => {
  const report = reportNode();
  assert.match(String(report.parameters.errorPayload ?? "={{ $('Lane Failed').first().json }}"), /Lane Failed/);
  assert.equal(errorsNode('Execution Lookup').onError, 'continueRegularOutput');
  assert.equal(errorsNode('Page Lifecycle').parameters.topic, 'lifecycle');
  assert.ok(!ERRORS.nodes.some((candidate) => /schedule|cron|interval/i.test(candidate.type)));
});
