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

test('the lane is exactly five workflows, all active', () => {
  const names = ['plane-bloodbank', 'plane-ingress-reconcile', 'ticket-grooming', 'ticket-delegation', 'ticket-pickup-chip'];
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

test('Plane Ingress Reconcile sweeps every 10 minutes and pushes each recovered ticket', () => {
  const wf = workflow('plane-ingress-reconcile');
  const [every] = node(wf, 'Every 10 Minutes').parameters.rule.interval;
  assert.equal(node(wf, 'Every 10 Minutes').type, 'n8n-nodes-base.scheduleTrigger');
  // Off the top of the hour and off the chip sweep's minute 51.
  assert.deepEqual(every, { field: 'cronExpression', expression: '3-59/10 * * * *' });
  const reconcile = node(wf, 'Reconcile Missed Tickets');
  assert.equal(reconcile.type, 'n8n-nodes-bloodbank.planeBloodbank');
  assert.equal(reconcile.typeVersion, 2); // Recovered + Report outputs
  assert.equal(reconcile.parameters.operation, 'reconcile');
  assert.ok(!reconcile.parameters.reconcile?.dryRun, 'the committed sweep publishes');
  // The chip's Plane key: one credential, one rate budget the sweep leaves room in.
  assert.equal(reconcile.credentials?.httpHeaderAuth?.name, 'Plane API (33GOD + AutomaticAI)');
  assert.deepEqual(out(wf, 'Every 10 Minutes', 0), ['Reconcile Missed Tickets']);
  assert.deepEqual(out(wf, 'Reconcile Missed Tickets', 0), ['Recovered Ticket']);
  assert.deepEqual(out(wf, 'Reconcile Missed Tickets', 1), []); // the sweep report stays in the execution
  assertLifecyclePush(wf, 'Recovered Ticket');
  assert.match(node(wf, 'Recovered Ticket').parameters.message, /^=Recovered missed ticket \{\{ \$json\.ticket_key/);
  // Same credential id as the chip's Plane calls.
  const chipCredential = node(workflow('ticket-pickup-chip'), 'Chip — Read Issue').credentials.httpHeaderAuth.id;
  assert.equal(reconcile.credentials.httpHeaderAuth.id, chipCredential);
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

// Chip — Plan Write's input is Chip — Read Activity (the ticket's activity,
// newest first); it pairs each item with Chip — Resolve Label (the target) and
// Chip — Read Issue (the ticket). Unless a test says otherwise, the history is
// just the chip going on at turn start.
const chipAdded = (at) => ({ results: [{ field: 'labels', verb: 'updated', old_value: '', new_value: 'agent:working', new_identifier: labelId, created_at: at }] });
function plan(targets, issues, activities) {
  const list = (value) => (Array.isArray(value) ? value : [value]);
  const issueList = list(issues);
  const acts = activities === undefined ? issueList.map(() => chipAdded('2026-09-23T04:40:00Z')) : list(activities);
  return run('Chip — Plan Write', acts.map((json) => ({ json })), {
    upstream: {
      'Chip — Resolve Label': list(targets).map((json) => ({ json })),
      'Chip — Read Issue': issueList.map((json) => ({ json })),
    },
  });
}

test('chip is a straight line with every Plane call guarded', () => {
  const line = ['Chip — Target', 'Chip — List Labels', 'Chip — Resolve Label', 'Chip — Read Issue',
    'Chip — Read Activity', 'Chip — Plan Write', 'Chip — Write Labels', 'Chip — Check Write'];
  for (let i = 0; i < line.length - 1; i++) assert.deepEqual(out(chip, line[i], 0), [line[i + 1]], line[i]);
  assert.deepEqual(out(chip, 'Invocation Lifecycle', 0), ['Chip — Target']);
  const planeCalls = chip.nodes.filter((n) => n.type === 'n8n-nodes-base.httpRequest' && n.parameters.url.includes('plane.delo.sh'));
  assert.equal(planeCalls.length, 4);
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
  const [every] = node(chip, 'Stale Chip Sweep').parameters.rule.interval;
  assert.equal(every.field, 'hours');
  assert.equal(every.hoursInterval, 1);
  // Pinned: without it n8n picks a random minute on every activation.
  assert.ok(Number.isInteger(every.triggerAtMinute) && every.triggerAtMinute >= 0 && every.triggerAtMinute < 60);
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
  // ...marked as a sweep, with the time the turn ended, for Chip — Plan Write.
  for (const t of targets) {
    assert.equal(t.json.sweep, true);
    const row = rows.find((r) => r.data.context.ticket_id === t.json.ticketId && /completed|failed/.test(r.type));
    assert.equal(t.json.eventTime, row.time);
  }
  assert.equal(run('Sweep — Ended Tickets', [{ json: { events: [] } }]).length, 0);
  assert.equal(run('Sweep — Ended Tickets', [{ json: {} }]).length, 0);
});

test('live lifecycle events are not sweeps', () => {
  const [live] = run('Chip — Target', [event('bloodbank.agent.invocation.completed', context)]);
  assert.equal(live.json.sweep, false);
  // A forged or stray `sweep: "yes"` does not turn the guard on or off by accident.
  const stray = { json: { ...event('bloodbank.agent.invocation.completed', context).json, sweep: 'yes' } };
  assert.equal(run('Chip — Target', [stray])[0].json.sweep, false);
});

// agent:working is also pilot's claim marker (`px claim` adds it, `px close`
// removes it). The sweep runs up to 48h after a turn, so it may only take off a
// chip nobody has touched since that turn ended.
test('the sweep leaves a chip alone on a ticket changed after the turn ended', () => {
  const ended = '2026-09-23T04:46:06.653596Z';
  const at = (offsetMs) => new Date(Date.parse(ended) + offsetMs).toISOString();
  const sweep = (extra = {}) => ({ action: 'remove', labelId, sweep: true, eventTime: ended, ...extra });
  const issue = (updated_at) => ({ labels: ['a', labelId], updated_at });

  // Delegation turn ends at T, the worker it spawned runs `px claim` at T+20min:
  // the claim marker stays.
  assert.equal(plan(sweep(), issue(at(20 * 60000))).length, 0);
  // A claim seconds after the turn survives too: the live lane took its chip
  // off ~0.5s after the end, so a label present later was put back by someone.
  assert.equal(plan(sweep(), issue(at(10000))).length, 0);
  assert.equal(plan(sweep(), issue(at(5001))).length, 0);
  // Untouched since the turn: the stuck chip comes off. Plane reports
  // updated_at in the server's local offset; that is the same instant.
  assert.deepEqual(plan(sweep(), issue(at(-5 * 60000)))[0].json.labels, ['a']);
  assert.deepEqual(plan(sweep(), issue(at(5000)))[0].json.labels, ['a']);
  assert.deepEqual(plan(sweep(), issue('2026-09-23T00:46:07.100000-04:00'))[0].json.labels, ['a']);
  // Unknown times prove nothing: the chip stays.
  assert.equal(plan(sweep(), issue(undefined)).length, 0);
  assert.equal(plan(sweep({ eventTime: null }), issue(at(0))).length, 0);
});

test('the live lane still removes at the real turn end, whatever updated_at says', () => {
  const live = { action: 'remove', labelId, sweep: false, eventTime: '2026-09-23T04:46:06Z' };
  const touched = { labels: ['a', labelId], updated_at: '2026-09-23T05:30:00Z' };
  assert.deepEqual(plan(live, touched)[0].json.labels, ['a']);
});

// A claim made WHILE the turn runs. The chip is already on (this lane put it
// there at turn start), so a worker's `px claim` -- or the delegation turn's own
// move to In Progress -- changes state and assignees but leaves no label change
// behind, and the turn's remove at the end used to strip the claim marker.
const T0 = '2026-09-23T10:00:01.000Z'; // this lane's own add at turn start
const later = (ms) => new Date(Date.parse(T0) + ms).toISOString();
const inProgress = { id: 'state-in-progress', name: 'In Progress', color: '#F59E0B', group: 'started' };
const backlog = { id: 'state-backlog', name: 'Backlog', color: '#60646C', group: 'backlog' };
const liveRemove = { action: 'remove', labelId, ticketKey: 'JIMB-1', ticketId: 'ticket', board, sweep: false, eventTime: later(5 * 60000) };
const ticket = (state, extra = {}) => ({ labels: ['a', labelId], state, updated_at: later(4 * 60000), ...extra });
// Plane serves activity newest first.
const history = (...rows) => ({ results: [...rows].reverse() });
const row = (field, ms, extra = {}) => ({ field, verb: 'updated', created_at: later(ms), ...extra });
const chipOn = row('labels', 0, { old_value: '', new_value: 'agent:working', new_identifier: labelId });
const toInProgress = (ms) => row('state', ms, { old_value: 'Backlog', new_value: 'In Progress', old_identifier: backlog.id, new_identifier: inProgress.id });
const assigned = (ms) => row('assignees', ms, { old_value: '', new_value: 'Jarad', new_identifier: 'user-1' });

test('a px claim made during the turn survives the turn end', () => {
  // px claim: one PATCH, state + assignee (+ the label, already on: no row).
  assert.equal(plan(liveRemove, ticket(inProgress), history(chipOn, toInProgress(90000), assigned(90002))).length, 0);
  // Either half is evidence on its own: the delegation turn claims by state
  // alone (its prompt says leave the assignee empty); a claim onto an already
  // In Progress ticket only assigns.
  assert.equal(plan(liveRemove, ticket(inProgress), history(chipOn, toInProgress(90000))).length, 0);
  assert.equal(plan(liveRemove, ticket(backlog), history(chipOn, assigned(60000))).length, 0);
  // A claim whose own PATCH put the label on (the chip's add had failed) is
  // seen too: its state row lands a few ms before its label row.
  const claimAdds = row('labels', 90003, { old_value: '', new_value: 'agent:working', new_identifier: labelId });
  assert.equal(plan(liveRemove, ticket(inProgress), history(toInProgress(90000), claimAdds)).length, 0);
});

test('a grooming turn (no move into started, no assignee) still loses its chip', () => {
  const groomed = history(
    chipOn,
    row('priority', 60000, { old_value: 'none', new_value: 'high' }),
    row('labels', 120000, { old_value: '', new_value: 'lifecycle:triaged', new_identifier: 'label-triaged' }),
    row('description', 130000),
  );
  assert.deepEqual(plan(liveRemove, ticket(backlog), groomed)[0].json.labels, ['a']);
  // A move between unstarted states is not a claim.
  const todo = { id: 'state-todo', name: 'Todo', group: 'unstarted' };
  const toTodo = row('state', 60000, { new_value: 'Todo', new_identifier: todo.id });
  assert.deepEqual(plan(liveRemove, ticket(todo), history(chipOn, toTodo))[0].json.labels, ['a']);
  // An assignee taken off is not an assignee added.
  const unassigned = row('assignees', 60000, { old_value: 'Jarad', new_value: '', old_identifier: 'user-1', new_identifier: null });
  assert.deepEqual(plan(liveRemove, ticket(backlog), history(chipOn, unassigned))[0].json.labels, ['a']);
  // A move into started that was later undone is not a standing claim.
  assert.deepEqual(plan(liveRemove, ticket(backlog), history(chipOn, toInProgress(60000)))[0].json.labels, ['a']);
});

test('a claim from before the chip went on is not this turn\'s claim', () => {
  const old = history(
    toInProgress(-2 * 3600 * 1000),
    row('labels', -2 * 3600 * 1000 + 1000, { old_value: 'agent:working', new_value: '', old_identifier: labelId, new_identifier: null }),
    chipOn,
  );
  assert.deepEqual(plan(liveRemove, ticket(inProgress), old)[0].json.labels, ['a']);
});

test('with no record of the chip going on, the current state decides', () => {
  assert.equal(plan(liveRemove, ticket(inProgress), { results: [] }).length, 0);
  assert.deepEqual(plan(liveRemove, ticket(backlog), { results: [] })[0].json.labels, ['a']);
  assert.deepEqual(plan(liveRemove, ticket(backlog), {})[0].json.labels, ['a']);
});

test('a claim made during the turn also keeps the sweep off it later', () => {
  const sweepRemove = { ...liveRemove, sweep: true };
  // updated_at is before the turn ended, so touchedSinceTurn alone would remove it.
  assert.equal(plan(sweepRemove, ticket(inProgress), history(chipOn, toInProgress(90000))).length, 0);
  assert.deepEqual(plan(sweepRemove, ticket(backlog), history(chipOn))[0].json.labels, ['a']);
});

test('an add never waits on the activity read', () => {
  const add = { action: 'add', labelId, ticketKey: 'JIMB-1', board };
  assert.deepEqual(plan(add, { labels: ['a'] }, { error: { httpCode: '502' } })[0].json.labels, ['a', labelId]);
});

test('Read Activity reads the newest page of the ticket history; Read Issue expands the state', () => {
  const url = node(chip, 'Chip — Read Activity').parameters.url;
  assert.match(url, /\/issues\/\{\{ \$\('Chip — Resolve Label'\)\.item\.json\.ticketId \}\}\/activities\/\?order_by=-created_at&per_page=100$/);
  assert.match(url, /workspaces\/\{\{ \$\('Chip — Resolve Label'\)\.item\.json\.ws \}\}/);
  assert.match(node(chip, 'Chip — Read Issue').parameters.url, /\/issues\/\{\{ \$json\.ticketId \}\}\/\?expand=state$/);
});

test('the sweep, end to end through the chip line, spares a claimed ticket', () => {
  const rows = [
    sweepRow('bloodbank.agent.invocation.started', 40, 'claimed'),
    sweepRow('bloodbank.agent.invocation.completed', 30, 'claimed'),
    sweepRow('bloodbank.agent.invocation.started', 40, 'stuck'),
    sweepRow('bloodbank.agent.invocation.completed', 30, 'stuck'),
  ];
  const targets = run('Chip — Target', run('Sweep — Ended Tickets', [{ json: { events: rows } }]));
  const resolved = targets.map((i) => ({ ...i.json, labelId }));
  const endedAt = Object.fromEntries(targets.map((i) => [i.json.ticketId, Date.parse(i.json.eventTime)]));
  const issues = targets.map((i) => ({
    id: i.json.ticketId,
    labels: [labelId],
    // 'claimed' was `px claim`ed 20 minutes after its turn; 'stuck' was not touched.
    updated_at: new Date(endedAt[i.json.ticketId] + (i.json.ticketId === 'claimed' ? 20 * 60000 : -1000)).toISOString(),
  }));
  const writes = plan(resolved, issues);
  assert.deepEqual(writes.map((w) => [w.json.ticketId, w.json.labels]), [['stuck', []]]);
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
  const resolved = (action) => ({ action, labelId });
  const issue = (labels) => ({ labels });
  assert.deepEqual(plan(resolved('add'), issue(['a']))[0].json.labels, ['a', labelId]);
  assert.equal(plan(resolved('add'), issue([labelId])).length, 0);
  assert.deepEqual(plan(resolved('remove'), issue(['a', labelId]))[0].json.labels, ['a']);
  assert.equal(plan(resolved('remove'), issue(['a'])).length, 0);
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
  ['Chip — Resolve Label', 'Chip — List Labels', (error) => run('Chip — Resolve Label', [{ json: { error } }], { upstream: { 'Chip — Target': [{ json: t }] } })],
  ['Chip — Plan Write', 'Chip — Read Issue', (error) => plan(t, { error })],
  // Only a remove reads the activity (an add never needs it).
  ['Chip — Plan Write', 'Chip — Read Activity', (error) => plan({ ...t, action: 'remove' }, { labels: [labelId] }, { error })],
  ['Chip — Check Write', 'Chip — Write Labels', (error) => run('Chip — Check Write', [{ json: { error } }], { upstream: { 'Chip — Plan Write': [{ json: t }] } })],
];

for (const [label, step, go] of guarded) {
  test(`${label}: a ticket deleted mid-turn (404/410 from ${step}) is a quiet skip`, () => {
    for (const [what, error] of gone) {
      assert.deepEqual(go(error), [], what);
    }
  });

  test(`${label}: any other ${step} failure still fails the execution, ticket named`, () => {
    for (const [what, error] of broken) {
      assert.throws(() => go(error), new RegExp(`${step} failed for JIMB-1`), what);
    }
  });
}

test('chip skips a soft-deleted ticket rather than writing to it', () => {
  assert.equal(plan(t, { labels: ['a'], deleted_at: '2026-09-22T00:00:00Z' }).length, 0);
});

test('chip check write passes clean writes and ends the line', () => {
  const upstream = { 'Chip — Plan Write': [{ json: t }] };
  assert.deepEqual(run('Chip — Check Write', [{ json: { id: 'ticket', labels: [labelId] } }], { upstream }), []);
});
