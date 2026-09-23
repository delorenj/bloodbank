// The lifecycle lane as it is actually wired in the four exported workflows
// under ../n8n-workflows: Plane → Bloodbank, Ticket Grooming, Ticket
// Delegation and the Ticket Pickup Chip. Wiring assertions read the exports;
// the chip's Code nodes are run the way n8n would run them.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const load = (name) =>
  JSON.parse(fs.readFileSync(path.join(__dirname, `../../n8n-workflows/${name}.v1.json`)));
const workflow = (name) => load(name)[0];
const node = (wf, name) => wf.nodes.find((n) => n.name === name);
// Target node names on output `index` of `from`.
const out = (wf, from, index) => ((wf.connections[from]?.main || [])[index] || []).map((c) => c.node);

const NTFY = 'n8n-nodes-ntfy-client.ntfySend';
const NTFY_CREDENTIAL = 'Ntfy account (n8n token)';

// ---------------------------------------------------------------------------
// Lane wiring
// ---------------------------------------------------------------------------

function assertLifecyclePush(wf, name) {
  const push = node(wf, name);
  assert.ok(push, `${wf.name}: missing ${name}`);
  assert.equal(push.type, NTFY);
  assert.equal(push.parameters.topic, 'lifecycle');
  assert.equal(push.credentials?.ntfyApi?.name, NTFY_CREDENTIAL);
  // A failed push must never fail the lane it reports on.
  assert.equal(push.onError, 'continueRegularOutput');
}

test('the lane is exactly four workflows, all active', () => {
  const names = ['plane-bloodbank', 'ticket-grooming', 'ticket-delegation', 'ticket-pickup-chip'];
  assert.deepEqual(
    fs.readdirSync(path.join(__dirname, '../../n8n-workflows')).filter((f) => f.endsWith('.v1.json')).sort(),
    names.map((n) => `${n}.v1.json`).sort(),
  );
  for (const name of names) assert.equal(workflow(name).active, true, name);
});

test('Plane → Bloodbank pages an unrouted board at most once a day', () => {
  const wf = workflow('plane-bloodbank');
  assert.equal(node(wf, 'Normalize and Publish').typeVersion, 2); // v2 has the Unrouted output
  assert.deepEqual(out(wf, 'Normalize and Publish', 1), ['Unrouted — Once a Day']);
  assert.deepEqual(out(wf, 'Unrouted — Once a Day', 0), ['Unrouted — First Today?']);
  assert.deepEqual(out(wf, 'Unrouted — First Today?', 0), ['Unrouted Board']);
  // A muted delivery must still end on a node with an item: the webhook
  // answers with the last node's output, and an empty one is an HTTP 500.
  assert.deepEqual(out(wf, 'Unrouted — First Today?', 1), ['Unrouted — Muted']);
  assert.equal(node(wf, 'Plane Webhook').parameters.responseMode, 'lastNode');
  assert.equal(node(wf, 'Unrouted — First Today?').parameters.conditions.conditions[0].leftValue, '={{ $json.notify }}');
  assertLifecyclePush(wf, 'Unrouted Board');
});

test('the once-a-day gate keys on the board and forgets after 24h', () => {
  const gate = node(workflow('plane-bloodbank'), 'Unrouted — Once a Day').parameters.jsCode;
  const store = {};
  const realNow = Date.now;
  let now = Date.parse('2026-09-23T00:00:00Z');
  Date.now = () => now;
  try {
    const runGate = (items) =>
      new Function('$input', '$getWorkflowStaticData', gate)({ all: () => items }, () => store);
    const delivery = (board) => ({ json: { board_id: board, plane_event: 'issue.created', reason: 'unrouted' } });
    const first = runGate([delivery('B1'), delivery('B1'), delivery('B2')]);
    assert.deepEqual(first.map((i) => i.json.notify), [true, false, true]);
    assert.equal(first[1].json.muted_until, '2026-09-24T00:00:00.000Z');
    assert.equal(first[0].json.board_id, 'B1'); // the page still has everything it prints
    now += 23 * 3600 * 1000;
    assert.deepEqual(runGate([delivery('B1')]).map((i) => i.json.notify), [false]);
    now += 2 * 3600 * 1000;
    assert.deepEqual(runGate([delivery('B1')]).map((i) => i.json.notify), [true]);
    assert.deepEqual(Object.keys(store.unroutedBoardPagedAt).sort(), ['B1'], 'expired boards are pruned');
  } finally {
    Date.now = realNow;
  }
});

test('Ticket Grooming pushes Dispatched and every Skipped item', () => {
  const wf = workflow('ticket-grooming');
  const groom = node(wf, 'Groom Ticket');
  assert.equal(groom.parameters.operation, 'groomTicket');
  assert.equal(groom.parameters.publishSkips, true); // bloodbank.agent.invocation.skipped
  assert.deepEqual(out(wf, 'Groom Ticket', 0), ['Triage Started']);
  assert.deepEqual(out(wf, 'Groom Ticket', 1), ['Triage Skipped']);
  assertLifecyclePush(wf, 'Triage Started');
  assertLifecyclePush(wf, 'Triage Skipped');
});

test('Ticket Delegation pushes Dispatched and notable Skipped items', () => {
  const wf = workflow('ticket-delegation');
  const delegate = node(wf, 'Delegate Ticket');
  assert.equal(delegate.parameters.operation, 'delegateTicket');
  assert.equal(delegate.parameters.publishSkips, true);
  assert.deepEqual(out(wf, 'Delegate Ticket', 0), ['Delegation Started']);
  assert.deepEqual(out(wf, 'Delegate Ticket', 1), ['Notable Skip?']);
  assert.deepEqual(out(wf, 'Notable Skip?', 0), ['Delegation Skipped']);
  // Routine guard misses are still published on the bus; they just do not page.
  const quiet = node(wf, 'Notable Skip?').parameters.conditions.conditions.map((c) => c.rightValue).sort();
  assert.deepEqual(quiet, ['phase_guard', 'provider_event_guard']);
  assertLifecyclePush(wf, 'Delegation Started');
  assertLifecyclePush(wf, 'Delegation Skipped');
});

for (const name of ['ticket-grooming', 'ticket-delegation']) {
  test(`${name} no longer carries the stateful Ack branch`, () => {
    const wf = workflow(name);
    assert.deepEqual(wf.nodes.filter((n) => /^Ack|Turn Ended/.test(n.name)), []);
    assert.ok(!JSON.stringify(wf).includes('$getWorkflowStaticData'));
  });
}

// ---------------------------------------------------------------------------
// Ticket Pickup Chip
// ---------------------------------------------------------------------------

const chip = workflow('ticket-pickup-chip');
const code = (label) => node(chip, label).parameters.jsCode;
const board = 'a8a12be1-b3ab-44f4-ab24-abe8829aeb72';
const labelId = 'label-agent-working';

function run(label, input, { fenced = [], upstream = {} } = {}) {
  const $env = fenced.length ? { KREBS_FENCED_BOARDS: JSON.stringify(fenced) } : {};
  const $ = (name) => ({ itemMatching: (i) => upstream[name][i] });
  return new Function('$env', '$input', '$', code(label))($env, { all: () => input }, $);
}

const event = (type, context) => ({
  json: { id: 'evt', type, correlationid: 'cid', data: { target_agent_id: '33god-pm', ...(context ? { context } : {}) } },
});
const context = { reason: 'ticket-grooming', workspace: 'automaticai', board_id: board, ticket_id: 'ticket', ticket_key: 'JIMB-1' };

test('chip is a straight line with every Plane call guarded', () => {
  const line = ['Chip — Target', 'Chip — List Labels', 'Chip — Resolve Label', 'Chip — Read Issue',
    'Chip — Plan Write', 'Chip — Write Labels', 'Chip — Check Write'];
  for (let i = 0; i < line.length - 1; i++) assert.deepEqual(out(chip, line[i], 0), [line[i + 1]], line[i]);
  assert.deepEqual(out(chip, 'Invocation Lifecycle', 0), ['Chip — Target']);
  const planeCalls = chip.nodes.filter((n) => n.type === 'n8n-nodes-base.httpRequest' && n.parameters.url.includes('plane.delo.sh'));
  assert.equal(planeCalls.length, 3);
  for (const http of planeCalls) {
    assert.equal(http.onError, 'continueRegularOutput', http.name);
  }
});

test('one durable trigger carries the whole turn, so started is handled before its end', () => {
  const triggers = chip.nodes.filter((n) => n.type === 'n8n-nodes-bloodbank.bloodbankTrigger');
  assert.equal(triggers.length, 1, 'two triggers would be two durables with no order between them');
  const [lifecycle] = triggers;
  assert.equal(lifecycle.name, 'Invocation Lifecycle');
  assert.deepEqual(lifecycle.parameters.events, [
    'bloodbank.agent.invocation.started',
    'bloodbank.agent.invocation.completed',
    'bloodbank.agent.invocation.failed',
  ]);
  // Durable delivery acknowledged after each execution is the default; the
  // export must not opt out of either.
  assert.ok(!('delivery' in lifecycle.parameters) || lifecycle.parameters.delivery === 'durable');
  assert.ok(!('acknowledge' in lifecycle.parameters) || lifecycle.parameters.acknowledge === 'afterExecution');
  assert.deepEqual(lifecycle.parameters.dataMatch.conditions, [
    { path: 'data.context.reason', values: 'ticket-grooming,ticket-delegation' },
  ]);
});

test('an hourly sweep over Candystore feeds ended turns into the chip line', () => {
  assert.equal(node(chip, 'Stale Chip Sweep').type, 'n8n-nodes-base.scheduleTrigger');
  assert.deepEqual(node(chip, 'Stale Chip Sweep').parameters.rule.interval, [{ field: 'hours', hoursInterval: 1 }]);
  assert.deepEqual(out(chip, 'Stale Chip Sweep', 0), ['Sweep — Ended Turns']);
  assert.deepEqual(out(chip, 'Sweep — Ended Turns', 0), ['Sweep — Ended Tickets']);
  assert.deepEqual(out(chip, 'Sweep — Ended Tickets', 0), ['Chip — Target']);
  const query = Object.fromEntries(
    node(chip, 'Sweep — Ended Turns').parameters.queryParameters.parameters.map((p) => [p.name, p.value]),
  );
  assert.equal(query.service, 'bloodbank-hermes-gateway');
  assert.deepEqual(query.type.split(','), [
    'bloodbank.agent.invocation.started',
    'bloodbank.agent.invocation.completed',
    'bloodbank.agent.invocation.failed',
  ]);
});

const sweepRow = (type, minutesAgo, ticket, extra = {}) => ({
  id: `${type}-${ticket}-${minutesAgo}`,
  type,
  time: new Date(Date.now() - minutesAgo * 60000).toISOString(),
  correlationid: `cid-${ticket}`,
  data: { target_agent_id: '33god-pm', context: { ...context, ticket_id: ticket, ...extra } },
});

test('the sweep removes only chips whose ticket\'s last turn ended and settled', () => {
  const rows = [
    sweepRow('bloodbank.agent.invocation.completed', 30, 'done'),
    sweepRow('bloodbank.agent.invocation.started', 40, 'done'),
    sweepRow('bloodbank.agent.invocation.started', 5, 'running'),
    sweepRow('bloodbank.agent.invocation.completed', 50, 'running'),
    sweepRow('bloodbank.agent.invocation.failed', 3, 'just-failed'),
    sweepRow('bloodbank.agent.invocation.failed', 90, 'failed'),
    sweepRow('bloodbank.agent.invocation.completed', 60, 'not-a-ticket-turn', { reason: 'cron' }),
    sweepRow('bloodbank.agent.invocation.completed', 60, 'no-board', { board_id: undefined }),
  ];
  const swept = run('Sweep — Ended Tickets', [{ json: { events: rows } }]);
  assert.deepEqual(swept.map((i) => i.json.data.context.ticket_id).sort(), ['done', 'failed']);
  // What it emits is what Chip — Target already understands: a remove.
  const targets = run('Chip — Target', swept);
  assert.deepEqual(targets.map((i) => i.json.action), ['remove', 'remove']);
  assert.equal(run('Sweep — Ended Tickets', [{ json: { events: [] } }]).length, 0);
  assert.equal(run('Sweep — Ended Tickets', [{ json: {} }]).length, 0);
});

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

// What the HTTP Request node (n8n 2.18) hands downstream under
// continueRegularOutput when Plane answers 404 — recorded from execution 241011,
// a started event for a ticket id that does not exist.
const plane404 = {
  message: '404 - "{\\"error\\":\\"The requested resource does not exist.\\"}"',
  name: 'AxiosError',
  code: 'ERR_BAD_REQUEST',
  status: 404,
};
const gone = [
  ['the recorded Axios 404', plane404],
  ['a NodeApiError 404', { httpCode: '404', description: 'Not found.' }],
  ['a bare message 410', { message: 'Request failed with status code 410' }],
];
const broken = [
  ['a 401', { httpCode: '401', message: 'Authorization failed' }],
  ['a 502', { statusCode: 502 }],
];
const t = { action: 'add', ws: 'automaticai', board, ticketId: 'ticket', ticketKey: 'JIMB-1', labelId };
const guarded = [
  ['Chip — Resolve Label', 'Chip — List Labels', { 'Chip — Target': [{ json: t }] }],
  ['Chip — Plan Write', 'Chip — Read Issue', { 'Chip — Resolve Label': [{ json: t }] }],
  ['Chip — Check Write', 'Chip — Write Labels', { 'Chip — Plan Write': [{ json: t }] }],
];

for (const [label, step, upstream] of guarded) {
  test(`${label}: a ticket deleted mid-turn (404/410 from ${step}) is a quiet skip`, () => {
    for (const [what, error] of gone) {
      assert.deepEqual(run(label, [{ json: { error } }], { upstream }), [], what);
    }
  });

  test(`${label}: any other ${step} failure still fails the execution, ticket named`, () => {
    for (const [what, error] of broken) {
      assert.throws(() => run(label, [{ json: { error } }], { upstream }), new RegExp(`${step} failed for JIMB-1`), what);
    }
  });
}

test('chip skips a soft-deleted ticket rather than writing to it', () => {
  const upstream = { 'Chip — Resolve Label': [{ json: t }] };
  assert.equal(run('Chip — Plan Write', [{ json: { labels: ['a'], deleted_at: '2026-09-22T00:00:00Z' } }], { upstream }).length, 0);
});

test('chip check write passes clean writes and ends the line', () => {
  const upstream = { 'Chip — Plan Write': [{ json: t }] };
  assert.deepEqual(run('Chip — Check Write', [{ json: { id: 'ticket', labels: [labelId] } }], { upstream }), []);
});
