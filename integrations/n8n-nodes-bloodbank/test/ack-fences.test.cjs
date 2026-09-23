// The agent:working chip moved out of the grooming/delegation lanes into the
// stateless "Ticket Pickup Chip" workflow, driven by the gateway's
// agent.invocation.started/completed/failed events and their data.context.
// These tests run that workflow's exported Code nodes the way n8n would.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const load = (name) =>
  JSON.parse(fs.readFileSync(path.join(__dirname, `../../n8n-workflows/${name}.v1.json`)));

const chip = load('ticket-pickup-chip').flatMap((w) => w.nodes);
const code = (label) => chip.find((n) => n.name === label).parameters.jsCode;
const board = 'a8a12be1-b3ab-44f4-ab24-abe8829aeb72';
const labelId = 'label-agent-working';

function run(label, input, { fenced = [], upstream = {} } = {}) {
  const $env = fenced.length ? { KREBS_FENCED_BOARDS: JSON.stringify(fenced) } : {};
  const $ = (node) => ({ itemMatching: (i) => upstream[node][i] });
  return new Function('$env', '$input', '$', code(label))($env, { all: () => input }, $);
}

const event = (type, context) => ({
  json: { id: 'evt', type, correlationid: 'cid', data: { target_agent_id: '33god-pm', ...(context ? { context } : {}) } },
});
const context = { reason: 'ticket-grooming', workspace: 'automaticai', board_id: board, ticket_id: 'ticket', ticket_key: 'JIMB-1' };

for (const name of ['ticket-grooming', 'ticket-delegation']) {
  test(`${name} no longer carries the stateful Ack branch`, () => {
    const [workflow] = load(name);
    assert.deepEqual(workflow.nodes.filter((n) => /^Ack|Turn Ended/.test(n.name)), []);
    assert.ok(!JSON.stringify(workflow).includes('$getWorkflowStaticData'));
  });
}

test('chip target maps started to add and completed/failed to remove', () => {
  const started = run('Chip — Target', [event('bloodbank.agent.invocation.started', context)]);
  assert.equal(started.length, 1);
  assert.deepEqual(
    { action: started[0].json.action, ws: started[0].json.ws, board: started[0].json.board, ticketId: started[0].json.ticketId },
    { action: 'add', ws: 'automaticai', board, ticketId: 'ticket' },
  );
  for (const type of ['bloodbank.agent.invocation.completed', 'bloodbank.agent.invocation.failed']) {
    assert.equal(run('Chip — Target', [event(type, context)])[0].json.action, 'remove');
  }
});

test('chip target drops turns with no ticket context', () => {
  assert.equal(run('Chip — Target', [event('bloodbank.agent.invocation.started')]).length, 0);
  assert.equal(
    run('Chip — Target', [event('bloodbank.agent.invocation.started', { ...context, ticket_id: undefined })]).length,
    0,
  );
});

test('chip target honors managed/shadow deployment fences', () => {
  const input = [event('bloodbank.agent.invocation.started', context)];
  assert.equal(run('Chip — Target', input, { fenced: [board] }).length, 0);
  assert.equal(run('Chip — Target', input, { fenced: ['some-other-board'] }).length, 1);
});

test('chip resolves the label by name and skips boards without one', () => {
  const target = { json: { action: 'add', ws: 'automaticai', board, ticketId: 'ticket' } };
  const upstream = { 'Chip — Target': [target] };
  const labels = { json: { results: [{ id: 'x', name: 'bug' }, { id: labelId, name: 'agent:working' }] } };
  assert.equal(run('Chip — Resolve Label', [labels], { upstream })[0].json.labelId, labelId);
  assert.equal(run('Chip — Resolve Label', [{ json: { results: [{ id: 'x', name: 'bug' }] } }], { upstream }).length, 0);
});

test('chip plans only real label changes', () => {
  const resolved = (action) => ({ 'Chip — Resolve Label': [{ json: { action, labelId } }] });
  const issue = (labels) => [{ json: { labels } }];
  assert.deepEqual(run('Chip — Plan Write', issue(['a']), { upstream: resolved('add') })[0].json.labels, ['a', labelId]);
  assert.equal(run('Chip — Plan Write', issue([labelId]), { upstream: resolved('add') }).length, 0);
  assert.deepEqual(run('Chip — Plan Write', issue(['a', labelId]), { upstream: resolved('remove') })[0].json.labels, ['a']);
  assert.equal(run('Chip — Plan Write', issue(['a']), { upstream: resolved('remove') }).length, 0);
});
