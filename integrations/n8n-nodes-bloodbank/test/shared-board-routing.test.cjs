const assert = require('node:assert/strict');
const test = require('node:test');

const {
  boardsFromProjectRegistry,
  classifyPlaneWebhook,
  mergePlaneRoutes,
  normalizePlaneWebhook,
  planeRoutesFromRegistry,
  resolveFleetAgentForBoard,
  routeForLabels,
  ticketFactsFromEnvelope,
} = require('../src/index.ts');

const SHARED = 'eb302959-0eac-48da-8fd5-ae1666bd500f';
const SOLO = '15258893-0206-4e8f-aea6-340eb217988c';
const INT_BOARD = '687535e9873b89478afef689';
const TICKET_ID = '5082ee4f-5e93-4fd5-8ee9-62ea4109b7fd';
const AT = '2026-11-02T12:00:00.000Z';

const plane = (extra = {}) => ({ type: 'plane', workspace: '33god', identifier: 'TOLT', board_id: SHARED, ...extra });

function projects(order = ['tolt', 'tower-of-dumb-things', 'lost-things-forge']) {
  const all = {
    tolt: { slug: 'tolt', ticket_provider: plane({ scope_label: 'repo:tolt' }) },
    'tower-of-dumb-things': {
      slug: 'tower-of-dumb-things',
      ticket_provider: plane({ scope_label: 'repo:game', scope_default: true }),
    },
    'lost-things-forge': { slug: 'lost-things-forge', ticket_provider: plane({ scope_label: 'repo:forge' }) },
    solo: { slug: 'solo', ticket_provider: { type: 'plane', workspace: '33god', identifier: 'SOLO', board_id: SOLO } },
  };
  return { projects: Object.fromEntries([...order, 'solo'].map((id) => [id, all[id]])) };
}

const HERMES = {
  agents: {
    'tolt-director': {
      repo: 'tolt',
      profile_name: 'tolt-director',
      plane: { workspace: '33god', project_id: SHARED, identifier: 'TOLT', scope_label: 'repo:tolt' },
      bloodbank: { enabled: true, gateway_scope: 'fleet', target_agent_id: 'tolt-director' },
    },
    'pile-of-dumb-things-pm': {
      repo: 'tower-of-lost-things',
      profile_name: 'pile-of-dumb-things-pm',
      plane: {
        workspace: '33god',
        project_id: SHARED,
        identifier: 'TOLT',
        scope_label: ['repo:game', 'repo:forge'],
        scope_default: true,
      },
      bloodbank: { enabled: true, gateway_scope: 'fleet', target_agent_id: 'pile-of-dumb-things-pm' },
    },
    'solo-pm': {
      repo: 'solo',
      profile_name: 'solo-pm',
      plane: { workspace: '33god', project_id: SOLO, identifier: 'SOLO' },
      bloodbank: { enabled: true, gateway_scope: 'fleet', target_agent_id: 'solo-pm' },
    },
  },
};

function table(order) {
  return mergePlaneRoutes(planeRoutesFromRegistry(HERMES), boardsFromProjectRegistry(projects(order)));
}

function issue(board, labels, extra = {}) {
  return {
    event: 'issue',
    action: 'create',
    workspace_slug: '33god',
    data: {
      id: TICKET_ID,
      project: board,
      sequence_id: 39,
      name: 'Ship it',
      created_at: AT,
      updated_at: AT,
      state_detail: { name: 'Todo', group: 'unstarted' },
      ...(labels === undefined ? {} : { labels }),
      ...extra,
    },
  };
}

const repoOf = (payload, routes = table()) => normalizePlaneWebhook(payload, routes, AT).data.repo;
const named = (...names) => names.map((name) => ({ id: `id-${name}`, name, color: '#000' }));

test('a repo:<scope> label routes the ticket to the project that declares it', () => {
  assert.equal(repoOf(issue(SHARED, named('repo:forge'))), 'lost-things-forge');
  assert.equal(repoOf(issue(SHARED, named('repo:game'))), 'tower-of-dumb-things');
  assert.equal(repoOf(issue(SHARED, named('repo:tolt'))), 'tolt');
  // Non-scope labels around it do not matter, and neither does case.
  assert.equal(repoOf(issue(SHARED, named('area:ui', 'Repo:Forge', 'bug'))), 'lost-things-forge');
  // Bare label names (not objects) work too.
  assert.equal(repoOf(issue(SHARED, ['repo:tolt'])), 'tolt');
});

test('no label routes to the board default, never to a lexicographic accident', () => {
  // `lost-things-forge` sorts first by name; the default flag must beat that.
  assert.equal(repoOf(issue(SHARED, undefined)), 'tower-of-dumb-things');
  assert.equal(repoOf(issue(SHARED, [])), 'tower-of-dumb-things');
  assert.equal(repoOf(issue(SHARED, named('area:ui', 'bug'))), 'tower-of-dumb-things');
});

test('an unknown scope label routes to the board default', () => {
  assert.equal(repoOf(issue(SHARED, named('repo:banana'))), 'tower-of-dumb-things');
});

test('ticket updates, transitions and deletes follow the same label routing', () => {
  const update = { ...issue(SHARED, named('repo:forge')), action: 'update' };
  const transition = {
    ...issue(SHARED, named('repo:tolt')),
    action: 'update',
    activity: { field: 'state', old_value: 'Backlog' },
  };
  const deleted = { ...issue(SHARED, named('repo:forge')), action: 'delete' };
  assert.equal(repoOf(update), 'lost-things-forge');
  assert.equal(repoOf(transition), 'tolt');
  assert.equal(repoOf(deleted), 'lost-things-forge');
  const event = normalizePlaneWebhook(update, table(), AT);
  assert.equal(event.data.slug, 'lost-things-forge');
  assert.equal(event.orderingKey, `task:lost-things-forge:${TICKET_ID}`);
  assert.equal(event.data.ticket_key, 'TOLT-39');
});

test('a comment carries no labels, so it lands on the board default', () => {
  const comment = {
    event: 'issue_comment',
    action: 'create',
    workspace_slug: '33god',
    data: { id: 'c-1', issue: TICKET_ID, project: SHARED, comment_html: '<p>hi</p>', created_at: AT },
  };
  assert.equal(repoOf(comment), 'tower-of-dumb-things');
});

test('a board-created event keeps the board default and is not label-routed', () => {
  const result = classifyPlaneWebhook(
    { event: 'project', action: 'create', workspace_slug: '33god', data: { id: SHARED, identifier: 'TOLT', name: 'T', created_at: AT } },
    table(),
    AT,
  );
  assert.equal(result.status, 'routed');
  assert.equal(result.event.data.repo, 'tower-of-dumb-things');
});

test('the choice does not depend on the order rows are read in', () => {
  const orders = [
    ['tolt', 'tower-of-dumb-things', 'lost-things-forge'],
    ['lost-things-forge', 'tolt', 'tower-of-dumb-things'],
    ['tower-of-dumb-things', 'lost-things-forge', 'tolt'],
  ];
  for (const order of orders) {
    const routes = table(order);
    for (const [labels, expected] of [
      [named('repo:forge'), 'lost-things-forge'],
      [named('repo:tolt'), 'tolt'],
      [named('repo:game'), 'tower-of-dumb-things'],
      [undefined, 'tower-of-dumb-things'],
    ]) {
      assert.equal(repoOf(issue(SHARED, labels), routes), expected, order.join());
    }
  }
  // Hermes rows too: reversing the registry changes nothing.
  const reversed = { agents: Object.fromEntries(Object.entries(HERMES.agents).reverse()) };
  const forward = planeRoutesFromRegistry(HERMES).get(SHARED);
  const backward = planeRoutesFromRegistry(reversed).get(SHARED);
  assert.deepEqual(backward, forward);
  assert.equal(forward.repo, 'tower-of-lost-things');
});

test('with no default declared, a shared board still resolves deterministically', () => {
  const rows = (order) => ({
    projects: Object.fromEntries(
      order.map((slug) => [slug, { slug, ticket_provider: plane({ scope_label: `repo:${slug}` }) }]),
    ),
  });
  const a = mergePlaneRoutes(new Map(), boardsFromProjectRegistry(rows(['b', 'a', 'c'])));
  const b = mergePlaneRoutes(new Map(), boardsFromProjectRegistry(rows(['c', 'b', 'a'])));
  assert.equal(a.get(SHARED).repo, 'a');
  assert.equal(b.get(SHARED).repo, 'a');
  assert.equal(repoOf(issue(SHARED, named('repo:c')), a), 'c');
});

test('a single-owner board is unchanged: same route shape, no scope needed', () => {
  const routes = table();
  const solo = routes.get(SOLO);
  assert.equal(solo.repo, 'solo');
  assert.equal(solo.source, 'pjangler');
  assert.equal(solo.owners, undefined);
  assert.equal(routeForLabels(solo, ['repo:forge']), solo);
  assert.equal(repoOf(issue(SOLO, named('repo:forge')), routes), 'solo');
  assert.equal(repoOf(issue(SOLO, undefined), routes), 'solo');
  // A Hermes-only single row keeps its exact old shape.
  const hermesOnly = planeRoutesFromRegistry({
    agents: { '33god-pm': { repo: '33god', plane: { project_id: SOLO, workspace: '33god', identifier: 'GOD' } } },
  });
  assert.deepEqual(hermesOnly.get(SOLO), {
    boardId: SOLO,
    repo: '33god',
    slug: '33god',
    workspace: '33god',
    boardKey: 'GOD',
    source: 'hermes',
  });
});

test('the IntelliForia shared Trello board is not a Plane route and stays that way', () => {
  const trello = (slug) => ({
    slug,
    ticket_provider: { type: 'trello', state: 'active', board_id: INT_BOARD, workspace: '', identifier: 'INT' },
  });
  const registry = { projects: { intelliforia: trello('intelliforia'), 'intelliforia-mobile': trello('intelliforia-mobile') } };
  assert.deepEqual(boardsFromProjectRegistry(registry), []);
  const routes = mergePlaneRoutes(planeRoutesFromRegistry(HERMES), boardsFromProjectRegistry(registry));
  assert.equal(routes.has(INT_BOARD), false);
  const result = classifyPlaneWebhook(issue(INT_BOARD, named('repo:game')), routes, AT);
  assert.equal(result.status, 'unrouted');
});

test('Plane projects sharing a board without scope labels route the same way every time', () => {
  const twin = (slug) => ({ slug, ticket_provider: plane() });
  const forward = mergePlaneRoutes(
    new Map(),
    boardsFromProjectRegistry({ projects: { b: twin('b'), a: twin('a') } }),
  );
  const backward = mergePlaneRoutes(
    new Map(),
    boardsFromProjectRegistry({ projects: { a: twin('a'), b: twin('b') } }),
  );
  assert.equal(forward.get(SHARED).repo, 'a');
  assert.equal(backward.get(SHARED).repo, 'a');
});

test('scope declarations travel in the pjangler registry block, as strings only', () => {
  const [board] = boardsFromProjectRegistry({
    projects: { x: { slug: 'x', ticket_provider: plane({ scope_label: ' Repo:Game ', scope_default: true }) } },
  });
  assert.deepEqual(board.scopeLabels, ['repo:game']);
  assert.equal(board.scopeDefault, true);
  const [bare] = boardsFromProjectRegistry({
    projects: { x: { slug: 'x', ticket_provider: plane({ scope_default: 'yes', scope_label: 7 }) } },
  });
  assert.equal(bare.scopeLabels, undefined);
  assert.equal(bare.scopeDefault, undefined);
});

// ---- Fleet: which agent drives the ticket ----

const fleetOf = (labels, board = SHARED, repo = '') =>
  resolveFleetAgentForBoard(HERMES, board, repo, labels);

test('fleet: the scope label picks the agent; no label or unknown label picks the default', () => {
  assert.equal(fleetOf(named('repo:tolt')).agentId, 'tolt-director');
  assert.equal(fleetOf(named('repo:game')).agentId, 'pile-of-dumb-things-pm');
  // repo:forge has no PM of its own: the game PM drives it.
  assert.equal(fleetOf(named('repo:forge')).agentId, 'pile-of-dumb-things-pm');
  assert.equal(fleetOf([]).agentId, 'pile-of-dumb-things-pm');
  assert.equal(fleetOf(undefined).agentId, 'pile-of-dumb-things-pm');
  assert.equal(fleetOf(named('repo:banana')).agentId, 'pile-of-dumb-things-pm');
  const route = fleetOf(named('repo:tolt'));
  assert.equal(route.matchedBy, 'board');
  assert.equal(route.eligible, true, route.why);
});

test('fleet: registry file order never decides', () => {
  const reversed = { agents: Object.fromEntries(Object.entries(HERMES.agents).reverse()) };
  for (const [labels, expected] of [
    [named('repo:tolt'), 'tolt-director'],
    [named('repo:forge'), 'pile-of-dumb-things-pm'],
    [[], 'pile-of-dumb-things-pm'],
  ]) {
    assert.equal(resolveFleetAgentForBoard(reversed, SHARED, '', labels).agentId, expected);
  }
  const undeclared = (order) => ({
    agents: Object.fromEntries(
      order.map((id) => [id, { repo: id, profile_name: id, plane: { project_id: SHARED }, bloodbank: { gateway_scope: 'fleet', target_agent_id: id } }]),
    ),
  });
  assert.equal(resolveFleetAgentForBoard(undeclared(['b', 'a']), SHARED, '').agentId, 'a');
  assert.equal(resolveFleetAgentForBoard(undeclared(['a', 'b']), SHARED, '').agentId, 'a');
});

test('fleet: a disabled owner is reported, not silently re-routed to the default', () => {
  const registry = JSON.parse(JSON.stringify(HERMES));
  registry.agents['tolt-director'].bloodbank.enabled = false;
  const route = resolveFleetAgentForBoard(registry, SHARED, '', named('repo:tolt'));
  assert.equal(route.agentId, 'tolt-director');
  assert.equal(route.eligible, false);
});

test('fleet: a single-agent board and the repo-pm fallback are unchanged', () => {
  const route = fleetOf(named('repo:tolt'), SOLO);
  assert.equal(route.agentId, 'solo-pm');
  assert.equal(route.eligible, true);
  const fallback = resolveFleetAgentForBoard(HERMES, 'ffffffff-0000-4000-8000-000000000000', 'solo');
  assert.equal(fallback.agentId, 'solo-pm');
  assert.equal(fallback.matchedBy, 'fallback');
});

test('ticket facts carry the label names the fleet routes on', () => {
  const event = normalizePlaneWebhook(issue(SHARED, named('repo:Forge', 'area:ui')), table(), AT);
  const facts = ticketFactsFromEnvelope({ data: event.data });
  assert.deepEqual(facts.labels, ['area:ui', 'repo:forge']);
  assert.equal(facts.repo, 'lost-things-forge');
  assert.equal(resolveFleetAgentForBoard(HERMES, facts.boardId, facts.repo, facts.labels).agentId, 'pile-of-dumb-things-pm');
  assert.deepEqual(ticketFactsFromEnvelope({ repo: 'x' }).labels, []);
});
