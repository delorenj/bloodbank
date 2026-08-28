const assert = require('node:assert/strict');
const test = require('node:test');

const {
  buildEnvelope,
  deterministicUuid,
  subjectFor,
} = require('../src/index.ts');

test('publisher builds a complete canonical event envelope', () => {
  const eventId = deterministicUuid('plane.ticket.created:ticket-1');
  const { subject, envelope } = buildEnvelope({
    type: 'bloodbank.repo.task.created',
    data: { repo: '33GOD', task_id: 'ticket-1', title: 'Test' },
    eventId,
    observedAt: '2026-08-26T12:34:56.000Z',
    extensions: {
      workspace: '33god',
      board_id: 'board-1',
      slug: '33GOD',
      provider_event_type: 'plane.ticket.created',
    },
  });
  assert.equal(subject, 'bloodbank.evt.repo.task.created');
  assert.equal(envelope.id, eventId);
  assert.equal(envelope.kind, 'event');
  assert.equal(envelope.domain, 'repo');
  assert.deepEqual(envelope.actor, {
    type: 'service',
    agent_id: 'bloodbank.integration.n8n',
  });
  assert.equal(envelope.ordering_key, 'repo:ticket-1');
  assert.equal(envelope.provider_event_type, 'plane.ticket.created');
  assert.equal(envelope.data.title, 'Test');
});

test('subject generation distinguishes events, commands, and replies', () => {
  const type = 'bloodbank.agent.invocation.start';
  assert.equal(subjectFor(type, 'event'), 'bloodbank.evt.agent.invocation.start');
  assert.equal(subjectFor(type, 'command'), 'bloodbank.cmd.agent.invocation.start');
  assert.equal(subjectFor(type, 'reply'), 'bloodbank.rpy.agent.invocation.start');
});

test('extensions cannot replace canonical envelope fields', () => {
  assert.throws(
    () => buildEnvelope({
      type: 'bloodbank.repo.task.created',
      data: { repo: '33GOD', title: 'Test' },
      extensions: { type: 'replacement' },
    }),
    /cannot replace a canonical envelope field/,
  );
});
