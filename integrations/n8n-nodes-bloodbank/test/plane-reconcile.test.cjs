// Plane ingress reconcile: the sweep that recovers ticket creations whose
// webhook never reached the bus (Plane does not retry an HTTP 5xx, and n8n
// receives the webhook itself, so every n8n restart can drop one).
const assert = require('node:assert/strict');
const { mkdtemp, rm, writeFile } = require('node:fs/promises');
const { join } = require('node:path');
const test = require('node:test');
const { stringify: stringifyYaml } = require('yaml');

const {
  PlaneBloodbank,
  PlaneRateLimited,
  buildEnvelope,
  classifyPlaneWebhook,
  clearProjectBoardCache,
  createdDedupeKey,
  createdFactTicketId,
  deterministicUuid,
  issueAsWebhookPayload,
  planReconcile,
  planeReader,
  planeRoutesFromRegistry,
  publish,
  sweepOrder,
  validateEnvelope,
} = require('../src/index.ts');

const WS_UUID = '9f478bc5-d7bc-4ab9-8435-5e646a58ef3f';
const BOARD_33GOD = '15258893-0206-4e8f-aea6-340eb217988c';
const BOARD_BB = '10d06f8d-c110-4ce5-beaa-0914534b090a';
const BOARD_OLD = '25aa04a1-0549-45c9-8899-d5791a44846e';
const BOARD_AAI = 'a8a12be1-b3ab-44f4-ab24-abe8829aeb72';
const NOW = new Date('2026-09-23T12:00:00.000Z');
const HOUR = 3600 * 1000;
const ago = (ms) => new Date(NOW.valueOf() - ms).toISOString();

const HERMES = {
  schema_version: 1,
  agents: {
    '33god-pm': { repo: '33god', plane: { project_id: BOARD_33GOD, workspace: '33god', identifier: '33GOD' } },
    'bloodbank-pm': { repo: 'bloodbank', plane: { project_id: BOARD_BB, workspace: '33god', identifier: 'BB' } },
    'old-pm': { repo: 'old', plane: { project_id: BOARD_OLD, workspace: '33god', identifier: 'GOD' } },
    'jimb-pm': { repo: 'james-brennan', plane: { project_id: BOARD_AAI, workspace: 'automaticai', identifier: 'JIMB' } },
  },
};
const routes = () => planeRoutesFromRegistry(HERMES);

// A ticket as Plane's REST API lists it with expand=state,labels,assignees.
function apiIssue(id, createdAgoMs, extra = {}) {
  return {
    id,
    name: `Ticket ${id}`,
    sequence_id: Number(String(id).replace(/\D/g, '')) || 1,
    project: BOARD_33GOD,
    workspace: WS_UUID,
    created_at: ago(createdAgoMs),
    updated_at: ago(createdAgoMs - 60_000),
    is_draft: false,
    archived_at: null,
    deleted_at: null,
    state: { id: 'state-backlog', name: 'Backlog', color: '#60646C', group: 'backlog', sequence: 1, default: true, project: BOARD_33GOD },
    labels: [{ id: 'label-1', name: 'bug', color: '#f00', description: 'x', sort_order: 1, workspace: WS_UUID }],
    assignees: [{ id: 'user-1', display_name: 'Jarad', email: 'j@example.test', first_name: 'Jarad', last_name: 'D', avatar: '', avatar_url: '', is_bot: false, role: 20 }],
    description_html: '<p>hi</p>',
    priority: 'none',
    ...extra,
  };
}

// A fake Plane: `boards` maps board id -> issues (any order; served newest first).
function fakePlane({ projects, boards = {}, perPage = 2, failOn = {} }) {
  const calls = [];
  return {
    calls,
    async projects(workspace) {
      calls.push(['projects', workspace]);
      if (failOn.projects) throw failOn.projects;
      return projects[workspace] || [];
    },
    async issuesPage(workspace, boardId, cursor) {
      calls.push(['issues', workspace, boardId, cursor]);
      if (failOn[boardId]) throw failOn[boardId];
      const sorted = [...(boards[boardId] || [])].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
      const start = cursor ? Number(cursor) : 0;
      const results = sorted.slice(start, start + perPage);
      const more = start + perPage < sorted.length;
      return { results, nextCursor: more ? String(start + perPage) : null, more };
    },
  };
}

const PROJECTS = {
  '33god': [
    { id: BOARD_33GOD, identifier: '33GOD', archived_at: null },
    { id: BOARD_BB, identifier: 'BB', archived_at: null },
    { id: BOARD_OLD, identifier: 'GOD', archived_at: '2026-06-24T11:35:35.839784-04:00' },
  ],
  automaticai: [{ id: BOARD_AAI, identifier: 'JIMB', archived_at: null }],
};

// ---------------------------------------------------------------------------
// The plan
// ---------------------------------------------------------------------------

test('the sweep finds creations in the window that the bus never heard about', async () => {
  const plane = fakePlane({
    projects: PROJECTS,
    boards: {
      [BOARD_33GOD]: [
        apiIssue('t1', 30 * 60_000), // missing: recover
        apiIssue('t2', 2 * HOUR), // already a fact
        apiIssue('t3', 30_000), // younger than settle: its webhook may be in flight
        apiIssue('t4', 7 * HOUR), // before the window
        apiIssue('t5', 3 * HOUR, { state: { id: 's', name: 'Done', group: 'completed' } }), // closed
        apiIssue('t6', 4 * HOUR, { is_draft: true }),
      ],
      [BOARD_BB]: [apiIssue('b1', HOUR, { project: BOARD_BB })],
      [BOARD_OLD]: [apiIssue('o1', HOUR, { project: BOARD_OLD })],
    },
  });
  let asked;
  const plan = await planReconcile({
    routes: routes(),
    plane,
    now: NOW,
    knownTicketIds: async (since) => {
      asked = since;
      return new Set(['t2']);
    },
  });
  assert.deepEqual(plan.candidates.map((c) => c.issue.id).sort(), ['b1', 't1']);
  assert.equal(plan.counts.already_on_bus, 1);
  assert.equal(plan.counts.too_young, 1);
  assert.equal(plan.counts.skipped_closed, 1);
  assert.equal(plan.counts.skipped_draft, 1);
  assert.equal(plan.counts.missing, 2);
  assert.equal(plan.partial, false);
  // Facts are searched from before the window, for clock skew.
  assert.equal(asked.toISOString(), ago(6 * HOUR + 10 * 60_000));
  assert.equal(plan.window.from, ago(6 * HOUR));
  assert.equal(plan.window.to, ago(2 * 60_000));
  // The archived board is reported and never listed.
  const old = plan.boards.find((b) => b.board_id === BOARD_OLD);
  assert.equal(old.status, 'skipped');
  assert.match(old.reason, /archived/);
  assert.ok(!plane.calls.some((call) => call[2] === BOARD_OLD));
  // One project list per workspace, however many boards it has.
  assert.equal(plane.calls.filter((call) => call[0] === 'projects' && call[1] === '33god').length, 1);
});

test('paging stops at the window start; the page budget marks a board truncated', async () => {
  const many = Array.from({ length: 9 }, (_, i) => apiIssue(`t${i + 1}`, (i + 1) * 20 * 60_000));
  const plane = fakePlane({ projects: PROJECTS, boards: { [BOARD_33GOD]: many }, perPage: 2 });
  const route = new Map([[BOARD_33GOD, routes().get(BOARD_33GOD)]]);
  const plan = await planReconcile({ routes: route, plane, now: NOW, lookbackMs: HOUR, knownTicketIds: async () => new Set() });
  // 20, 40 and 60 minutes old are inside one hour; the 80-minute ticket ends the read.
  assert.deepEqual(plan.candidates.map((c) => c.issue.id), ['t1', 't2', 't3']);
  assert.equal(plane.calls.filter((call) => call[0] === 'issues').length, 2);
  assert.equal(plan.boards[0].truncated, undefined);

  const capped = fakePlane({ projects: PROJECTS, boards: { [BOARD_33GOD]: many }, perPage: 2 });
  const short = await planReconcile({
    routes: route, plane: capped, now: NOW, lookbackMs: 6 * HOUR, maxPagesPerBoard: 2, knownTicketIds: async () => new Set(),
  });
  assert.equal(capped.calls.filter((call) => call[0] === 'issues').length, 2);
  assert.equal(short.boards[0].truncated, true);
  assert.equal(short.candidates.length, 4);
});

test('with nothing in the window the bus is not asked at all', async () => {
  const plane = fakePlane({ projects: PROJECTS, boards: { [BOARD_33GOD]: [apiIssue('t1', 9 * HOUR)] } });
  let asked = false;
  const plan = await planReconcile({ routes: routes(), plane, now: NOW, knownTicketIds: async () => { asked = true; return new Set(); } });
  assert.equal(asked, false);
  assert.equal(plan.counts.missing, 0);
});

test('a rate-limited sweep stops, reports the rest unchecked, and rotates next time', async () => {
  const plane = fakePlane({
    projects: PROJECTS,
    boards: { [BOARD_33GOD]: [apiIssue('t1', HOUR)] },
    failOn: { [BOARD_BB]: new PlaneRateLimited('Plane answered 429') },
  });
  const order = sweepOrder(routes(), NOW.valueOf()).map((r) => r.boardId);
  const plan = await planReconcile({ routes: routes(), plane, now: NOW, knownTicketIds: async () => new Set() });
  assert.equal(plan.partial, true);
  assert.match(plan.stopped_reason, /429/);
  const after = order.slice(order.indexOf(BOARD_BB) + 1);
  for (const board of after) assert.equal(plan.boards.find((b) => b.board_id === board).status, 'unchecked');
  assert.ok(!plane.calls.some((call) => after.includes(call[2])), 'nothing is read after the stop');
  // The order rotates with the time slot, so a sweep that keeps getting cut
  // short still reaches every board.
  const firsts = new Set();
  for (let slot = 0; slot < 4; slot++) firsts.add(sweepOrder(routes(), slot * 10 * 60_000)[0].boardId);
  assert.equal(firsts.size, 4);
});

test('a board Plane will not list, or a route with no workspace slug, is skipped and named', async () => {
  const table = routes();
  table.set('deadbeef-0000-4000-8000-000000000000', { boardId: 'deadbeef-0000-4000-8000-000000000000', repo: 'ghost', slug: 'ghost', workspace: '33god' });
  table.set('cafebabe-0000-4000-8000-000000000000', { boardId: 'cafebabe-0000-4000-8000-000000000000', repo: 'nows', slug: 'nows', workspace: 'unknown' });
  const plane = fakePlane({ projects: PROJECTS });
  const plan = await planReconcile({ routes: table, plane, now: NOW, knownTicketIds: async () => new Set() });
  assert.match(plan.boards.find((b) => b.repo === 'ghost').reason, /does not list/);
  assert.match(plan.boards.find((b) => b.repo === 'nows').reason, /no workspace slug/);
});

test('a board that fails to read is reported without sinking the sweep', async () => {
  const plane = fakePlane({
    projects: PROJECTS,
    boards: { [BOARD_33GOD]: [apiIssue('t1', HOUR)] },
    failOn: { [BOARD_BB]: new Error('Plane GET /issues answered HTTP 502') },
  });
  const plan = await planReconcile({ routes: routes(), plane, now: NOW, knownTicketIds: async () => new Set() });
  assert.equal(plan.boards.find((b) => b.board_id === BOARD_BB).status, 'error');
  assert.deepEqual(plan.candidates.map((c) => c.issue.id), ['t1']);
  assert.equal(plan.partial, false);
});

// ---------------------------------------------------------------------------
// Same normalizer, same fact
// ---------------------------------------------------------------------------

// What Plane's webhook delivers for the same ticket (recorded shape: momo MOMO-7).
function webhookDelivery(issue) {
  return {
    event: 'issue',
    action: 'created',
    webhook_id: '24bc401a-00fa-46cd-bfff-65e14ca1707a',
    workspace_id: WS_UUID,
    workspace_slug: '33god',
    data: {
      ...issue,
      // The webhook stamps updated_at a hair before created_at on a creation.
      updated_at: new Date(Date.parse(issue.created_at) - 8).toISOString(),
      state: { id: 'state-backlog', name: 'Backlog', color: '#60646C', group: 'backlog' },
      labels: [{ id: 'label-1', name: 'bug', color: '#f00' }],
      assignees: [{ id: 'user-1', email: 'j@example.test', avatar: '', last_name: 'D', avatar_url: '', first_name: 'Jarad', display_name: 'Jarad' }],
    },
  };
}

test('a recovered creation is the fact the webhook would have published', () => {
  const issue = apiIssue('t1', HOUR, { updated_at: ago(5 * 60_000) }); // groomed since: updated_at moved
  const route = routes().get(BOARD_33GOD);
  const recovered = classifyPlaneWebhook(issueAsWebhookPayload(issue, route), routes());
  const delivered = classifyPlaneWebhook(webhookDelivery(apiIssue('t1', HOUR)), routes());
  assert.equal(recovered.status, 'routed');
  assert.equal(delivered.status, 'routed');
  const a = recovered.event;
  const b = delivered.event;
  // One ticket, one creation: same key, same event id, same time.
  assert.equal(a.dedupeKey, createdDedupeKey(BOARD_33GOD, 't1'));
  assert.equal(a.dedupeKey, b.dedupeKey);
  assert.equal(deterministicUuid(a.dedupeKey), deterministicUuid(b.dedupeKey));
  assert.equal(a.observedAt, new Date(issue.created_at).toISOString());
  assert.equal(a.observedAt, b.observedAt);
  assert.equal(a.canonicalType, 'bloodbank.repo.task.created');
  assert.equal(a.providerEventType, 'plane.ticket.created');
  assert.equal(a.orderingKey, b.orderingKey);
  assert.deepEqual(a.extensions, b.extensions);
  // Same data shape; only the provenance of the observation differs.
  assert.deepEqual(Object.keys(a.data).sort(), Object.keys(b.data).sort());
  assert.equal(a.data.trigger_source, 'plane-reconcile');
  assert.equal(b.data.trigger_source, 'plane-webhook');
  for (const key of ['repo', 'slug', 'workspace', 'board_id', 'project_id', 'ticket_id', 'task_id', 'ticket_key', 'title', 'phase', 'tp_band', 'provider', 'timestamp']) {
    assert.deepEqual(a.data[key], b.data[key], key);
  }
  assert.equal(a.data.ticket_key, '33GOD-1');
  // Expanded API entities are trimmed to the webhook's fields.
  assert.deepEqual(a.data.ticket.state, b.data.ticket.state);
  assert.deepEqual(a.data.ticket.labels, b.data.ticket.labels);
  assert.deepEqual(a.data.ticket.assignees, b.data.ticket.assignees);
  // And it is a schema-valid envelope.
  const { envelope } = buildEnvelope({
    type: a.canonicalType,
    data: a.data,
    eventId: deterministicUuid(a.dedupeKey),
    observedAt: a.observedAt,
    orderingKey: a.orderingKey,
    extensions: a.extensions,
  });
  assert.doesNotThrow(() => validateEnvelope(a.canonicalType, envelope));
});

test('updates keep their per-observation key; only creation is keyed on the ticket alone', () => {
  const delivery = { ...webhookDelivery(apiIssue('t1', HOUR)), action: 'updated' };
  const event = classifyPlaneWebhook(delivery, routes()).event;
  assert.notEqual(event.dedupeKey, createdDedupeKey(BOARD_33GOD, 't1'));
  assert.equal(event.data.trigger_source, 'plane-webhook');
});

test('a stored creation fact yields its ticket id; anything unreadable yields nothing', () => {
  assert.equal(createdFactTicketId(JSON.stringify({ data: { ticket_id: 't1' } })), 't1');
  assert.equal(createdFactTicketId(Buffer.from(JSON.stringify({ data: { task_id: 't2' } }))), 't2');
  assert.equal(createdFactTicketId('not json'), undefined);
  assert.equal(createdFactTicketId(JSON.stringify({ data: {} })), undefined);
});

// ---------------------------------------------------------------------------
// Nats-Msg-Id: a race between webhook and sweep is one message
// ---------------------------------------------------------------------------

test('an event published with a msgId carries it as Nats-Msg-Id', async () => {
  const sent = [];
  const connectNats = async () => ({
    publish(subject, data, opts) { sent.push({ subject, opts }); },
    flush: async () => {},
    drain: async () => {},
  });
  const id = deterministicUuid(createdDedupeKey(BOARD_33GOD, 't1'));
  await publish({ type: 'bloodbank.repo.task.created', data: { repo: 'x' }, eventId: id, msgId: id }, connectNats);
  await publish({ type: 'bloodbank.repo.task.created', data: { repo: 'x' } }, connectNats);
  assert.equal(sent[0].opts.headers.get('Nats-Msg-Id'), id);
  assert.equal(sent[1].opts, undefined);
});

// ---------------------------------------------------------------------------
// The Plane reader
// ---------------------------------------------------------------------------

function fakeFetch(responses) {
  const requests = [];
  const impl = async (url, init) => {
    requests.push({ url, headers: init.headers });
    const next = responses.shift() || { status: 200, body: { results: [] } };
    return {
      status: next.status,
      ok: next.status >= 200 && next.status < 300,
      headers: { get: (name) => (next.headers || {})[name.toLowerCase()] ?? null },
      json: async () => next.body,
    };
  };
  return { impl, requests };
}

test('the reader lists newest-first with the credential header, paced', async () => {
  const { impl, requests } = fakeFetch([
    { status: 200, body: { results: [{ id: 'a' }], next_cursor: '100:1:0', next_page_results: true }, headers: { 'x-ratelimit-remaining': '50' } },
    { status: 200, body: { results: [{ id: 'b' }], next_cursor: null, next_page_results: false } },
  ]);
  const sleeps = [];
  let clock = 1000;
  const reader = planeReader({
    baseUrl: 'https://plane.test/',
    header: { name: 'X-API-Key', value: 'k' },
    fetchImpl: impl,
    paceMs: 250,
    now: () => clock,
    sleep: async (ms) => { sleeps.push(ms); clock += ms; },
  });
  const first = await reader.issuesPage('33god', BOARD_33GOD, null);
  const second = await reader.issuesPage('33god', BOARD_33GOD, first.nextCursor);
  assert.deepEqual(first, { results: [{ id: 'a' }], nextCursor: '100:1:0', more: true });
  assert.equal(second.more, false);
  const url = new URL(requests[0].url);
  assert.equal(url.pathname, `/api/v1/workspaces/33god/projects/${BOARD_33GOD}/issues/`);
  assert.equal(url.searchParams.get('order_by'), '-created_at');
  assert.equal(url.searchParams.get('expand'), 'state,labels,assignees');
  assert.equal(new URL(requests[1].url).searchParams.get('cursor'), '100:1:0');
  assert.equal(requests[0].headers['X-API-Key'], 'k');
  assert.deepEqual(sleeps, [250]);
});

test('the reader stops at 429, and at the reserve before Plane has to say so', async () => {
  const limited = fakeFetch([{ status: 429, body: {} }]);
  const reader = planeReader({ header: { name: 'X-API-Key', value: 'k' }, fetchImpl: limited.impl, paceMs: 0 });
  await assert.rejects(() => reader.projects('33god'), PlaneRateLimited);
  await assert.rejects(() => reader.projects('33god'), PlaneRateLimited);
  assert.equal(limited.requests.length, 1, 'no request after a 429');

  const low = fakeFetch([{ status: 200, body: { results: [] }, headers: { 'x-ratelimit-remaining': '10' } }]);
  const careful = planeReader({ header: { name: 'X-API-Key', value: 'k' }, fetchImpl: low.impl, paceMs: 0, rateReserve: 10 });
  await careful.projects('33god');
  await assert.rejects(() => careful.projects('33god'), /down to 10 requests/);
  assert.equal(low.requests.length, 1);

  const broken = fakeFetch([{ status: 401, body: {} }]);
  const denied = planeReader({ header: { name: 'X-API-Key', value: 'k' }, fetchImpl: broken.impl, paceMs: 0 });
  await assert.rejects(() => denied.projects('33god'), /HTTP 401/);
  assert.throws(() => planeReader({ header: { name: '', value: '' } }), /no header name or value/);
});

// ---------------------------------------------------------------------------
// The node's Reconcile operation
// ---------------------------------------------------------------------------

async function reconcileHarness(t, { plane, known = new Set(), settings = {}, version = 2, publishError }) {
  const dir = await mkdtemp(join(__dirname, '..', 'node_modules', '.plane-reconcile-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const registryFile = join(dir, 'agents-registry.yaml');
  await writeFile(registryFile, stringifyYaml(HERMES), 'utf8');
  const parameters = {
    operation: 'reconcile',
    reconcile: settings,
    registryFile,
    routing: { projectRegistry: 'http://pjangler.test' },
    connection: {},
  };
  const context = {
    getInputData: () => [{ json: {} }],
    getNodeParameter(name, _index, fallback) {
      return Object.prototype.hasOwnProperty.call(parameters, name) ? parameters[name] : fallback;
    },
    getNode: () => ({ name: 'Reconcile Missed Tickets', type: 'n8n-nodes-bloodbank.planeBloodbank', typeVersion: version, parameters: {} }),
    getCredentials: async () => { throw new Error('the fake reader needs no credential'); },
    continueOnFail: () => false,
  };
  const published = [];
  const deps = {
    publish: async (options) => {
      if (publishError) throw publishError;
      published.push(options);
      return { subject: `bloodbank.evt.${options.type.slice('bloodbank.'.length)}`, eventId: options.eventId, correlationid: options.correlationId };
    },
    fetchProjectRegistry: async () => ({ schema_version: 1, projects: {} }),
    planeReader: plane,
    knownTicketIds: async () => known,
    now: NOW,
  };
  const outputs = await PlaneBloodbank.prototype.execute.call(context, deps);
  return { outputs, published };
}

test('Reconcile publishes each missing creation through the webhook path and reports the sweep', async (t) => {
  clearProjectBoardCache();
  const plane = fakePlane({
    projects: PROJECTS,
    boards: { [BOARD_33GOD]: [apiIssue('t1', HOUR), apiIssue('t2', 2 * HOUR)] },
  });
  const { outputs, published } = await reconcileHarness(t, { plane, known: new Set(['t2']) });
  const [recovered, report] = outputs;
  assert.equal(recovered.length, 1);
  assert.equal(published.length, 1);
  const [fact] = published;
  const id = deterministicUuid(createdDedupeKey(BOARD_33GOD, 't1'));
  assert.equal(fact.type, 'bloodbank.repo.task.created');
  assert.equal(fact.eventId, id);
  assert.equal(fact.msgId, id, 'Nats-Msg-Id is the event id');
  // Envelope identity of a webhook-born fact.
  assert.equal(fact.source, 'urn:33god:integration:n8n:plane-webhook');
  assert.equal(fact.producer, 'n8n-plane-webhook');
  assert.deepEqual(fact.actor, { type: 'ticket_provider', agent_id: 'bloodbank.integration.plane', provider: 'plane' });
  assert.equal(fact.data.provider_event_type, 'plane.ticket.created');
  assert.equal(fact.data.trigger_source, 'plane-reconcile');
  assert.equal(fact.observedAt, ago(HOUR));
  assert.deepEqual(
    { key: recovered[0].json.ticket_key, recovered: recovered[0].json.recovered, event: recovered[0].json.event_id },
    { key: '33GOD-1', recovered: true, event: id },
  );
  assert.equal(recovered[0].json.url, 'https://plane.delo.sh/33god/browse/33GOD-1/');
  assert.equal(report.length, 1);
  assert.equal(report[0].json.recovered, 1);
  assert.equal(report[0].json.already_on_bus, 1);
  assert.deepEqual(report[0].json.recovered_tickets, ['33GOD-1']);
});

test('a second sweep over the same tickets publishes nothing', async (t) => {
  clearProjectBoardCache();
  const plane = () => fakePlane({ projects: PROJECTS, boards: { [BOARD_33GOD]: [apiIssue('t1', HOUR)] } });
  const first = await reconcileHarness(t, { plane: plane() });
  assert.equal(first.published.length, 1);
  const second = await reconcileHarness(t, { plane: plane(), known: new Set(['t1']) });
  assert.equal(second.published.length, 0);
  assert.equal(second.outputs[0].length, 0);
  assert.equal(second.outputs[1][0].json.already_on_bus, 1);
});

test('a dry run reports what it would recover and publishes nothing', async (t) => {
  clearProjectBoardCache();
  const plane = fakePlane({ projects: PROJECTS, boards: { [BOARD_33GOD]: [apiIssue('t1', HOUR)] } });
  const { outputs, published } = await reconcileHarness(t, { plane, settings: { dryRun: true } });
  assert.equal(published.length, 0);
  assert.equal(outputs[0][0].json.dry_run, true);
  assert.equal(outputs[0][0].json.recovered, false);
  assert.equal(outputs[0][0].json.event_id, deterministicUuid(createdDedupeKey(BOARD_33GOD, 't1')));
});

test('a sweep that can read no board at all fails; a bus outage fails it too', async (t) => {
  clearProjectBoardCache();
  const dead = fakePlane({ projects: PROJECTS, failOn: { projects: new Error('Plane GET /projects answered HTTP 401') } });
  await assert.rejects(() => reconcileHarness(t, { plane: dead }), /read no board: .*401/);
  const plane = fakePlane({ projects: PROJECTS, boards: { [BOARD_33GOD]: [apiIssue('t1', HOUR)] } });
  await assert.rejects(() => reconcileHarness(t, { plane, publishError: new Error('NATS down') }), /NATS down/);
});

test('v1 answers recovered tickets and the report on its single output', async (t) => {
  clearProjectBoardCache();
  const plane = fakePlane({ projects: PROJECTS, boards: { [BOARD_33GOD]: [apiIssue('t1', HOUR)] } });
  const { outputs } = await reconcileHarness(t, { plane, version: 1 });
  assert.equal(outputs.length, 1);
  assert.equal(outputs[0].length, 2);
  assert.equal(outputs[0][1].json.operation, 'reconcile');
});

test('the node declares the Reconcile operation, its credential and its outputs', () => {
  const { description } = new PlaneBloodbank();
  const operation = description.properties.find((p) => p.name === 'operation');
  assert.equal(operation.default, 'webhook', 'saved webhook workflows keep working unchanged');
  assert.deepEqual(operation.options.map((o) => o.value), ['webhook', 'reconcile']);
  assert.deepEqual(description.credentials, [
    { name: 'httpHeaderAuth', required: true, displayOptions: { show: { operation: ['reconcile'] } } },
  ]);
  assert.match(String(description.outputs), /Recovered/);
  assert.match(String(description.outputs), /Report/);
  const verify = description.properties.find((p) => p.name === 'verifySignature');
  assert.deepEqual(verify.displayOptions.show.operation, ['webhook']);
});
