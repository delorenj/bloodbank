const assert = require('node:assert/strict');
const test = require('node:test');

const {
  BloodbankTrigger,
  commandSchemas,
  eventSchemas,
  normalizePlaneWebhook,
  planeBindingMatches,
  planeRoutesFromRegistry,
} = require('../src/index.ts');

const BOARD_ID = 'f6746659-7698-4b5a-b509-9666e35fab09';
const TICKET_ID = '5082ee4f-5e93-4fd5-8ee9-62ea4109b7fd';
const OBSERVED_AT = '2026-08-26T12:34:56.000Z';

function routes() {
  return planeRoutesFromRegistry({
    agents: {
      '33god-pm': {
        repo: '33GOD',
        plane: { project_id: BOARD_ID, workspace: '33god', identifier: '33GOD' },
      },
    },
  });
}

test('schema codegen exposes events and exactly one command selector surface', () => {
  assert.ok(eventSchemas.some((schema) => schema.type === 'bloodbank.v1.repo.task.updated'));
  assert.ok(eventSchemas.some((schema) => schema.type === 'bloodbank.v1.repo.task.appended'));
  assert.ok(commandSchemas.some((schema) => schema.type === 'bloodbank.v1.agent.invocation.start'));
  assert.ok(commandSchemas.every((schema) => schema.kind === 'command'));

  const description = new BloodbankTrigger().description;
  const eventProperty = description.properties.find((property) => property.name === 'events');
  const commandProperty = description.properties.find((property) => property.name === 'command');
  const processingProperty = description.properties.find(
    (property) => property.name === 'commandProcessing',
  );
  assert.equal(eventProperty.type, 'multiOptions');
  assert.equal(commandProperty.type, 'options');
  assert.deepEqual(processingProperty.displayOptions.show.messageKind, ['command']);
  for (const alias of [
    'plane.board.created',
    'plane.ticket.created',
    'plane.ticket.updated',
    'plane.ticket.transitioned',
    'plane.ticket.commented',
    'plane.ticket.deleted',
  ]) {
    assert.ok(eventProperty.options.some((option) => option.value === alias), alias);
  }
});

test('Plane issue.created normalizes to a canonical repo.task.created fact', () => {
  const event = normalizePlaneWebhook(
    {
      event: 'issue',
      action: 'created',
      data: {
        id: TICKET_ID,
        project: BOARD_ID,
        sequence_id: 38,
        name: 'Integrate Plane with Bloodbank',
        created_at: OBSERVED_AT,
        state_detail: { name: 'In Progress', group: 'started' },
      },
    },
    routes(),
    OBSERVED_AT,
  );
  assert.ok(event);
  assert.equal(event.canonicalType, 'bloodbank.v1.repo.task.created');
  assert.equal(event.providerEventType, 'plane.ticket.created');
  assert.equal(event.data.repo, '33GOD');
  assert.equal(event.data.slug, '33GOD');
  assert.equal(event.data.workspace, '33god');
  assert.equal(event.data.board_id, BOARD_ID);
  assert.equal(event.data.ticket_id, TICKET_ID);
  assert.equal(event.data.ticket_key, '33GOD-38');
  assert.equal(event.data.tp_band, 'started');
  assert.deepEqual(event.data.ticket.state_detail, { name: 'In Progress', group: 'started' });
  assert.ok(planeBindingMatches('plane.ticket.created', {
    type: event.canonicalType,
    data: event.data,
  }));
  assert.equal(planeBindingMatches('plane.ticket.transitioned', {
    type: event.canonicalType,
    data: event.data,
  }), false);
});

test('Plane state changes preserve raw ticket JSON and normalize transition provenance', () => {
  const event = normalizePlaneWebhook(
    {
      event: 'issue',
      action: 'updated',
      activity: {
        field: 'state',
        old_value: { name: 'Todo', group: 'unstarted' },
      },
      data: {
        id: TICKET_ID,
        project: BOARD_ID,
        sequence_id: 38,
        name: 'Integrate Plane with Bloodbank',
        updated_at: OBSERVED_AT,
        state_detail: { name: 'In Progress', group: 'started' },
      },
    },
    routes(),
    OBSERVED_AT,
  );
  assert.ok(event);
  assert.equal(event.canonicalType, 'bloodbank.v1.repo.task.updated');
  assert.equal(event.providerEventType, 'plane.ticket.transitioned');
  assert.deepEqual(event.data.changed_fields, ['state']);
  assert.equal(event.data.previous_phase, 'Todo');
  assert.equal(event.data.previous_tp_band, 'unstarted');
  assert.equal(event.data.phase, 'In Progress');
  assert.equal(event.data.tp_band, 'started');
});

test('Plane comments normalize to append-only repo.task.appended facts', () => {
  const event = normalizePlaneWebhook(
    {
      event: 'issue_comment',
      action: 'created',
      data: {
        id: 'comment-1',
        issue: TICKET_ID,
        project: BOARD_ID,
        comment_html: '<p>Evidence attached.</p>',
        created_by: 'user-1',
        created_at: OBSERVED_AT,
      },
    },
    routes(),
    OBSERVED_AT,
  );
  assert.ok(event);
  assert.equal(event.canonicalType, 'bloodbank.v1.repo.task.appended');
  assert.equal(event.providerEventType, 'plane.ticket.commented');
  assert.equal(event.data.comment_id, 'comment-1');
  assert.equal(event.data.body, '<p>Evidence attached.</p>');
  assert.equal(event.data.comment.issue, TICKET_ID);
});

test('new Plane project webhooks can emit board provenance before registry reconciliation', () => {
  const event = normalizePlaneWebhook(
    {
      event: 'project',
      action: 'created',
      data: {
        id: 'board-new',
        identifier: 'NEW',
        name: 'New Project',
        workspace: { slug: '33god' },
        created_at: OBSERVED_AT,
      },
    },
    new Map(),
    OBSERVED_AT,
  );
  assert.ok(event);
  assert.equal(event.canonicalType, 'bloodbank.v1.repo.board.created');
  assert.equal(event.providerEventType, 'plane.board.created');
  assert.equal(event.data.slug, 'new');
  assert.equal(event.data.workspace, '33god');
  assert.equal(event.data.board.id, 'board-new');
});
