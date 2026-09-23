const assert = require('node:assert/strict');
const { createHmac } = require('node:crypto');
const { mkdtemp, readFile, rm, writeFile } = require('node:fs/promises');
const { join } = require('node:path');
const test = require('node:test');
const { stringify: stringifyYaml } = require('yaml');

const {
  PLANE_PROVIDER_EVENT_TYPES,
  PlaneBloodbank,
  boardFromManifest,
  boardsFromProjectRegistry,
  buildEnvelope,
  cachedSecret,
  classifyPlaneWebhook,
  clearProjectBoardCache,
  clearSecretCache,
  loadProjectBoards,
  mayForceRefresh,
  mergePlaneRoutes,
  normalizePlaneWebhook,
  planeEventBindings,
  planeRoutesFromRegistry,
  projectRegistryLocation,
  providerAliases,
  unboundRegistryProjectPaths,
  updateDedupeKey,
  validateEnvelope,
} = require('../src/index.ts');

const WS_UUID = '9f478bc5-d7bc-4ab9-8435-5e646a58ef3f';
const BOARD_33GOD = '15258893-0206-4e8f-aea6-340eb217988c';
const BOARD_BB = '10d06f8d-c110-4ce5-beaa-0914534b090a';
const BOARD_FLUME = 'bf5663f0-8d37-41c6-97a3-437d45d64523';
const BOARD_TIKT = '3e35184f-9cf6-4b0a-bf42-46a5a8e142e1';
const BOARD_UNKNOWN = '7e2557f9-861f-4cc7-8929-9b7f514c7fc3';
const TICKET_ID = '5082ee4f-5e93-4fd5-8ee9-62ea4109b7fd';
const WEBHOOK_ID = '24bc401a-00fa-46cd-bfff-65e14ca1707a';
const SECRET_REF = 'op://DeLoSecrets/PlaneWebhook-33GOD/credential';
const AT = '2026-09-22T12:00:00.000Z';

const HERMES = {
  schema_version: 1,
  agents: {
    '33god-pm': { repo: '33god', plane: { project_id: BOARD_33GOD, workspace: '33god', identifier: '33GOD' } },
    'bloodbank-pm': { repo: 'bloodbank', plane: { project_id: BOARD_BB, workspace: '33god', identifier: 'BB' } },
  },
};

const PJANGLER = {
  schema_version: 1,
  projects: {
    bb: { slug: 'bb', repo_path: '/code/bloodbank', ticket_provider: { type: 'plane', board_id: BOARD_BB, workspace: '33god', identifier: 'BB' } },
    flume: { slug: 'flume', project_id: 'flume', ticket_provider: { type: 'plane', board_id: BOARD_FLUME, workspace: '33god', identifier: 'FLUME' } },
    intelliforia: { slug: 'intelliforia', ticket_provider: { type: 'trello', board_id: '687535e9873b89478afef689' } },
    vinyl: { slug: 'vinyl', ticket_provider: { workspace: '33god', identifier: 'VINY' } },
  },
};

function issue(action, board, extra = {}) {
  return {
    event: 'issue',
    action,
    webhook_id: WEBHOOK_ID,
    workspace_id: WS_UUID,
    workspace_slug: '33god',
    data: {
      id: TICKET_ID,
      project: board,
      workspace: WS_UUID,
      sequence_id: 7,
      name: 'Ship it',
      created_at: AT,
      updated_at: AT,
      state_detail: { name: 'Todo', group: 'unstarted' },
      ...extra,
    },
  };
}

// ---------------------------------------------------------------------------
// The schema is the single source of provider aliases
// ---------------------------------------------------------------------------

test('every provider_event_type the normalizer can emit is declared in a schema', () => {
  const declared = new Map(providerAliases.map((alias) => [alias.value, alias.canonicalType]));
  for (const value of Object.values(PLANE_PROVIDER_EVENT_TYPES)) {
    assert.ok(declared.has(value), `${value} is not declared in any schema's x-provider-aliases`);
  }
});

test('no Plane provenance literal hides in the normalizer source undeclared', async () => {
  const source = await readFile(join(__dirname, '..', 'src', 'plane.ts'), 'utf8');
  const literals = new Set(source.match(/'plane\.[a-z_]+\.[a-z_]+'/g).map((literal) => literal.slice(1, -1)));
  const declared = new Set(providerAliases.map((alias) => alias.value));
  for (const literal of literals) assert.ok(declared.has(literal), `${literal} is not schema-declared`);
});

test('each emitted provider event lands on the canonical type its schema declares', () => {
  const routes = planeRoutesFromRegistry(HERMES);
  const scenarios = [
    [issue('create', BOARD_33GOD), 'plane.ticket.created'],
    [issue('update', BOARD_33GOD, {}), 'plane.ticket.updated'],
    [{ ...issue('update', BOARD_33GOD), activity: { field: 'state', old_value: { name: 'Backlog', group: 'backlog' } } }, 'plane.ticket.transitioned'],
    [issue('delete', BOARD_33GOD), 'plane.ticket.deleted'],
    [{ event: 'issue_comment', action: 'create', workspace_slug: '33god', data: { id: 'c-1', issue: TICKET_ID, project: BOARD_33GOD, comment_html: '<p>hi</p>', created_at: AT } }, 'plane.ticket.commented'],
    [{ event: 'project', action: 'create', workspace_slug: '33god', data: { id: BOARD_UNKNOWN, identifier: 'NEW', name: 'New', workspace: WS_UUID, created_at: AT } }, 'plane.board.created'],
  ];
  const seen = new Set();
  for (const [payload, expected] of scenarios) {
    const event = normalizePlaneWebhook(payload, routes, AT);
    assert.ok(event, expected);
    assert.equal(event.providerEventType, expected);
    const alias = providerAliases.find((candidate) => candidate.value === expected);
    assert.equal(event.canonicalType, alias.canonicalType);
    seen.add(expected);

    // And the fact is a schema-valid envelope, required fields and all.
    const { envelope } = buildEnvelope({
      type: event.canonicalType,
      data: event.data,
      eventId: '6ba7b810-9dad-11d1-80b4-00c04fd430c8',
      observedAt: event.observedAt,
      orderingKey: event.orderingKey,
      extensions: event.extensions,
    });
    assert.doesNotThrow(() => validateEnvelope(event.canonicalType, envelope), expected);
  }
  assert.deepEqual([...seen].sort(), Object.values(PLANE_PROVIDER_EVENT_TYPES).sort());
});

test('Plane trigger aliases are derived from the schema, in the shared option shape', () => {
  assert.equal(planeEventBindings.length, 6);
  const created = planeEventBindings.find((binding) => binding.value === 'plane.ticket.created');
  assert.equal(created.canonicalType, 'bloodbank.repo.task.created');
  assert.equal(created.name, 'Plane · On Ticket Created (plane.ticket.created)');
});

// ---------------------------------------------------------------------------
// Classification
// ---------------------------------------------------------------------------

test('a supported event on a board nobody claims is unrouted, with its reason', () => {
  const result = classifyPlaneWebhook(issue('create', BOARD_UNKNOWN), planeRoutesFromRegistry(HERMES), AT);
  assert.equal(result.status, 'unrouted');
  assert.equal(result.boardId, BOARD_UNKNOWN);
  assert.equal(result.workspace, '33god');
  assert.match(result.reason, /no enrolled project claims/);
});

test('an event Bloodbank does not model is unsupported, not unrouted', () => {
  const routes = planeRoutesFromRegistry(HERMES);
  for (const payload of [
    { event: 'project', action: 'update', data: { id: BOARD_33GOD } },
    { event: 'cycle', action: 'create', data: { id: 'x', project: BOARD_33GOD } },
    { event: 'issue', action: 'create', data: {} },
  ]) {
    assert.equal(classifyPlaneWebhook(payload, routes, AT).status, 'unsupported');
  }
});

test('the workspace is the slug even when the payload carries the UUID', () => {
  const hermes = { agents: { x: { repo: 'x', plane: { project_id: BOARD_33GOD, identifier: 'X' } } } };
  const event = normalizePlaneWebhook(issue('create', BOARD_33GOD), planeRoutesFromRegistry(hermes), AT);
  assert.equal(event.data.workspace, '33god');
  assert.equal(event.extensions.workspace, '33god');
});

test('an unclaimed board.created carries repo=null and a slug workspace', () => {
  const event = normalizePlaneWebhook(
    { event: 'project', action: 'create', workspace_slug: '33god', data: { id: BOARD_UNKNOWN, identifier: 'NEW', name: 'New', workspace: WS_UUID } },
    new Map(),
    AT,
  );
  assert.equal(event.data.repo, null);
  assert.equal(event.data.workspace, '33god');
  assert.equal(event.data.board_key, 'NEW');
});

test('a claimed board.created names its repo', () => {
  const event = normalizePlaneWebhook(
    { event: 'project', action: 'create', workspace_slug: '33god', data: { id: BOARD_BB, identifier: 'BB', name: 'Bloodbank' } },
    planeRoutesFromRegistry(HERMES),
    AT,
  );
  assert.equal(event.data.repo, 'bloodbank');
});

// ---------------------------------------------------------------------------
// Routing: pjangler enrollment merged with the Hermes org chart
// ---------------------------------------------------------------------------

test('pjangler enrollment yields Plane boards only, keyed by slug', () => {
  const boards = boardsFromProjectRegistry(PJANGLER);
  assert.deepEqual(boards.map((board) => board.boardId).sort(), [BOARD_BB, BOARD_FLUME].sort());
  assert.equal(boards.find((board) => board.boardId === BOARD_BB).repo, 'bb');
});

test('merged routes: pjangler wins on the repo slug, Hermes fills the rest', () => {
  const routes = mergePlaneRoutes(planeRoutesFromRegistry(HERMES), boardsFromProjectRegistry(PJANGLER), [
    { boardId: BOARD_TIKT, repo: 'tiktoktrivia', workspace: '33god', identifier: 'TIKT', source: 'manifest' },
  ]);
  assert.equal(routes.get(BOARD_BB).repo, 'bb');
  assert.equal(routes.get(BOARD_BB).source, 'pjangler');
  assert.equal(routes.get(BOARD_33GOD).repo, '33god');
  assert.equal(routes.get(BOARD_33GOD).source, 'hermes');
  assert.equal(routes.get(BOARD_FLUME).repo, 'flume');
  assert.equal(routes.get(BOARD_TIKT).repo, 'tiktoktrivia');
  assert.equal(routes.get(BOARD_TIKT).source, 'manifest');
  // An enrolled board without an agent now publishes facts.
  const event = normalizePlaneWebhook(issue('create', BOARD_FLUME), routes, AT);
  assert.equal(event.data.repo, 'flume');
  assert.equal(event.data.ticket_key, 'FLUME-7');
});

test('a Hermes row without a board contributes its manifest', async (t) => {
  const dir = await mkdtemp(join(__dirname, '..', 'node_modules', '.manifest-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  await writeFile(join(dir, '.project.json'), JSON.stringify({
    project_id: 'tiktoktrivia',
    ticket_provider: { type: 'plane', workspace: '33god', identifier: 'TIKT', board_id: BOARD_TIKT },
  }));
  const hermes = { agents: { dumply: { repo: 'tiktoktrivia', project_path: dir } } };
  assert.deepEqual(unboundRegistryProjectPaths(hermes), [dir]);
  const board = await boardFromManifest(dir);
  assert.equal(board.boardId, BOARD_TIKT);
  assert.equal(board.repo, 'tiktoktrivia');
  assert.equal(await boardFromManifest(join(dir, 'missing')), undefined);
});

test('the project registry location follows pjangler\'s own resolution order', () => {
  assert.equal(projectRegistryLocation('', {}), 'http://localhost:8764');
  assert.equal(projectRegistryLocation('', { PJ_REGISTRY_URL: 'http://r:1' }), 'http://r:1');
  assert.equal(projectRegistryLocation('', { PJ_PROJECT_REGISTRY: '/f.json', PJ_REGISTRY_URL: 'http://r:1' }), '/f.json');
  assert.equal(projectRegistryLocation('http://x:2', { PJ_REGISTRY_URL: 'http://r:1' }), 'http://x:2');
});

test('project boards are cached, served stale on failure, and absent when never read', async () => {
  clearProjectBoardCache();
  let calls = 0;
  const ok = async () => { calls += 1; return PJANGLER; };
  const down = async () => { calls += 1; throw new Error('ECONNREFUSED'); };

  const never = await loadProjectBoards({ location: 'http://t', fetcher: down, now: 0 });
  assert.equal(never.status, 'unavailable');
  assert.deepEqual(never.boards, []);

  const fresh = await loadProjectBoards({ location: 'http://t', fetcher: ok, now: 1000 });
  assert.equal(fresh.status, 'fresh');
  const cached = await loadProjectBoards({ location: 'http://t', fetcher: ok, now: 2000 });
  assert.equal(cached.status, 'cache');
  assert.equal(calls, 2);

  const stale = await loadProjectBoards({ location: 'http://t', fetcher: down, now: 1000 + 60_000 });
  assert.equal(stale.status, 'stale');
  assert.equal(stale.boards.length, 2);
  assert.match(stale.error, /ECONNREFUSED/);
  clearProjectBoardCache();
});

// ---------------------------------------------------------------------------
// Secret cache
// ---------------------------------------------------------------------------

test('a secret is read once per TTL, shared by concurrent misses, and served stale on error', async () => {
  clearSecretCache();
  let reads = 0;
  const read = async () => { reads += 1; await new Promise((r) => setTimeout(r, 5)); return 's3cret'; };
  const [a, b] = await Promise.all([
    cachedSecret('op://v/i/f', read, { now: 0 }),
    cachedSecret('op://v/i/f', read, { now: 0 }),
  ]);
  assert.equal(reads, 1);
  assert.equal(a.value, 's3cret');
  assert.equal(b.value, 's3cret');
  assert.equal((await cachedSecret('op://v/i/f', read, { now: 30 * 60 * 1000 })).source, 'cache');
  assert.equal(reads, 1);

  const failing = async () => { reads += 1; throw new Error('rate limited'); };
  const stale = await cachedSecret('op://v/i/f', failing, { now: 2 * 60 * 60 * 1000 });
  assert.equal(stale.source, 'stale');
  assert.equal(stale.value, 's3cret');
  assert.match(stale.error, /rate limited/);

  clearSecretCache();
  await assert.rejects(() => cachedSecret('op://v/i/f', failing, { now: 0 }), /rate limited/);
});

test('forced refreshes are rate-limited per reference', () => {
  clearSecretCache();
  assert.equal(mayForceRefresh('op://a', 0), true);
  assert.equal(mayForceRefresh('op://a', 1000), false);
  assert.equal(mayForceRefresh('op://b', 1000), true);
  assert.equal(mayForceRefresh('op://a', 6 * 60 * 1000), true);
  clearSecretCache();
});

// ---------------------------------------------------------------------------
// Update keys: one save, several deliveries
// ---------------------------------------------------------------------------

// Plane sends one webhook per activity row, and every row of one save carries
// the ticket's same updated_at. Recorded shape: activity {field, old_value,
// new_value, old_identifier, new_identifier, actor}.
function updateDelivery(activity, extra = {}) {
  return { ...issue('updated', BOARD_33GOD, extra), activity: { actor: { id: 'user-1' }, ...activity } };
}

test('two updates in one save (same updated_at, same state) are two facts, not one', () => {
  const routes = planeRoutesFromRegistry(HERMES);
  const labels = normalizePlaneWebhook(updateDelivery({ field: 'labels', new_value: 'bug', new_identifier: 'label-bug' }), routes, AT);
  const assignee = normalizePlaneWebhook(updateDelivery({ field: 'assignees', new_value: 'jarad', new_identifier: 'user-1' }), routes, AT);
  const secondLabel = normalizePlaneWebhook(updateDelivery({ field: 'labels', new_value: 'agent:working', new_identifier: 'label-chip' }), routes, AT);
  const removed = normalizePlaneWebhook(updateDelivery({ field: 'labels', old_value: 'bug', old_identifier: 'label-bug' }), routes, AT);
  const keys = [labels, assignee, secondLabel, removed].map((event) => event.dedupeKey);
  assert.equal(new Set(keys).size, 4, keys.join('\n'));
  // The changed fields are in the key, sorted.
  assert.match(labels.dedupeKey, /:Todo:labels:>label-bug$/);
  assert.equal(
    updateDedupeKey('plane.ticket.updated', BOARD_33GOD, TICKET_ID, AT, 'Todo', ['priority', 'labels']),
    `plane.ticket.updated:${BOARD_33GOD}:${TICKET_ID}:${AT}:Todo:labels,priority:`,
  );
  // A redelivery of the same delivery is the same fact.
  const again = normalizePlaneWebhook(updateDelivery({ field: 'labels', new_value: 'bug', new_identifier: 'label-bug' }), routes, AT);
  assert.equal(again.dedupeKey, labels.dedupeKey);
  // Values without identifiers still tell rows apart.
  const low = normalizePlaneWebhook(updateDelivery({ field: 'priority', old_value: 'none', new_value: 'low' }), routes, AT);
  const high = normalizePlaneWebhook(updateDelivery({ field: 'priority', old_value: 'none', new_value: 'high' }), routes, AT);
  assert.notEqual(low.dedupeKey, high.dedupeKey);
});

test('Nats-Msg-Id is claimed only by facts whose key names the fact itself', () => {
  const routes = planeRoutesFromRegistry(HERMES);
  const stable = (payload) => normalizePlaneWebhook(payload, routes, AT).stableId;
  assert.equal(stable(issue('create', BOARD_33GOD)), true);
  assert.equal(stable(issue('delete', BOARD_33GOD)), true);
  assert.equal(stable({ event: 'issue_comment', action: 'create', workspace_slug: '33god', data: { id: 'c-1', issue: TICKET_ID, project: BOARD_33GOD, created_at: AT } }), true);
  assert.equal(stable({ event: 'project', action: 'create', workspace_slug: '33god', data: { id: BOARD_UNKNOWN, identifier: 'NEW', created_at: AT } }), true);
  assert.equal(stable(updateDelivery({ field: 'labels', new_identifier: 'label-bug' })), false);
  assert.equal(stable(updateDelivery({ field: 'state', new_identifier: 'state-2' })), false);
});

// ---------------------------------------------------------------------------
// The node
// ---------------------------------------------------------------------------

async function nodeHarness(t, { version, deliveries, secret = 'plane-secret', readSecret }) {
  const dir = await mkdtemp(join(__dirname, '..', 'node_modules', '.plane-node-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const registryFile = join(dir, 'agents-registry.yaml');
  await writeFile(registryFile, stringifyYaml(HERMES), 'utf8');
  const bodies = deliveries.map((payload) => Buffer.from(JSON.stringify(payload)));
  const items = deliveries.map((payload, index) => ({
    json: {
      headers: { 'x-plane-signature': createHmac('sha256', secret).update(bodies[index]).digest('hex') },
      body: payload,
    },
    binary: { data: {} },
  }));
  const parameters = {
    verifySignature: true,
    webhookSecretReferences: JSON.stringify({ [WEBHOOK_ID]: SECRET_REF }),
    registryFile,
    routing: { projectRegistry: 'http://pjangler.test' },
    connection: {},
  };
  const context = {
    getInputData: () => items,
    getNodeParameter(name, _index, fallback) {
      return Object.prototype.hasOwnProperty.call(parameters, name) ? parameters[name] : fallback;
    },
    getNode: () => ({ name: 'Normalize and Publish', type: 'n8n-nodes-bloodbank.planeBloodbank', typeVersion: version, parameters: {} }),
    continueOnFail: () => false,
    helpers: { getBinaryDataBuffer: async (index) => bodies[index] },
  };
  const published = [];
  let secretReads = 0;
  const deps = {
    publish: async (options) => {
      published.push(options);
      return { subject: `bloodbank.evt.${options.type.slice('bloodbank.'.length)}`, eventId: options.eventId, correlationid: options.correlationId };
    },
    readSecret: readSecret || (async () => { secretReads += 1; return secret; }),
    fetchProjectRegistry: async () => PJANGLER,
  };
  const outputs = await PlaneBloodbank.prototype.execute.call(context, deps);
  return { outputs, published, secretReads: () => secretReads };
}

test('v2 splits unclaimed boards onto the Unrouted output and publishes the rest', async (t) => {
  clearSecretCache();
  clearProjectBoardCache();
  const { outputs, published, secretReads } = await nodeHarness(t, {
    version: 2,
    deliveries: [issue('create', BOARD_FLUME), issue('create', BOARD_UNKNOWN), issue('create', BOARD_BB)],
  });
  assert.equal(outputs.length, 2);
  const [main, unrouted] = outputs;
  assert.equal(main.length, 2);
  assert.equal(unrouted.length, 1);
  assert.equal(unrouted[0].json.board_id, BOARD_UNKNOWN);
  assert.equal(unrouted[0].json.unrouted, true);
  assert.equal(unrouted[0].json.plane_event, 'issue.created');
  assert.equal(main[0].json.secret_source, 'fresh');
  assert.equal(unrouted[0].json.secret_source, 'cache');
  assert.deepEqual(published.map((options) => options.data.repo), ['flume', 'bb']);
  // Webhook facts carry Nats-Msg-Id = event id, the same key the reconcile
  // sweep derives, so a race between the two is one message on the stream.
  for (const options of published) {
    assert.equal(options.msgId, options.eventId);
    assert.equal(options.data.trigger_source, 'plane-webhook');
  }
  assert.equal(main[0].json.route_source, 'pjangler');
  // Three signed deliveries, one vault read.
  assert.equal(secretReads(), 1);
  clearSecretCache();
});

test('the node sends Nats-Msg-Id for a creation and none for an update', async (t) => {
  clearSecretCache();
  clearProjectBoardCache();
  const { published } = await nodeHarness(t, {
    version: 2,
    deliveries: [issue('create', BOARD_33GOD), updateDelivery({ field: 'labels', new_identifier: 'label-bug' })],
  });
  assert.equal(published.length, 2);
  assert.equal(published[0].msgId, published[0].eventId);
  assert.equal(published[1].msgId, undefined);
  assert.ok(published[1].eventId, 'an update still has its deterministic event id');
  clearSecretCache();
});

test('v1 keeps one output and answers an unclaimed board on it, as before', async (t) => {
  clearSecretCache();
  clearProjectBoardCache();
  const { outputs } = await nodeHarness(t, { version: 1, deliveries: [issue('create', BOARD_UNKNOWN)] });
  assert.equal(outputs.length, 1);
  assert.equal(outputs[0][0].json.routed, false);
  assert.equal(outputs[0][0].json.unrouted, true);
  clearSecretCache();
});

test('a rotated secret recovers with one forced re-read', async (t) => {
  clearSecretCache();
  clearProjectBoardCache();
  await cachedSecret(SECRET_REF, async () => 'old-secret');
  let reads = 0;
  const { outputs } = await nodeHarness(t, {
    version: 2,
    deliveries: [issue('create', BOARD_33GOD)],
    secret: 'new-secret',
    readSecret: async () => { reads += 1; return 'new-secret'; },
  });
  assert.equal(outputs[0][0].json.routed, true);
  assert.equal(reads, 1);
  clearSecretCache();
});

test('a bad signature still fails before anything is published', async (t) => {
  clearSecretCache();
  clearProjectBoardCache();
  await assert.rejects(
    () => nodeHarness(t, {
      version: 2,
      deliveries: [issue('create', BOARD_33GOD)],
      secret: 'forged',
      readSecret: async () => 'the-real-secret',
    }),
    /signature mismatch/,
  );
  clearSecretCache();
});

test('the node declares version-dependent outputs', () => {
  const { description } = new PlaneBloodbank();
  assert.deepEqual(description.version, [1, 2]);
  assert.equal(description.defaultVersion, 2);
  assert.match(String(description.outputs), /\$nodeVersion >= 2/);
  assert.match(String(description.outputs), /Unrouted/);
  const registryFile = description.properties.find((property) => property.name === 'registryFile');
  assert.equal(registryFile.type, 'hidden');
});
