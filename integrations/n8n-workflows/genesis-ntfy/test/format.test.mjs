import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { workflow, EVENTS } from '../build.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const formatNtfy = new Function(`${readFileSync(join(here, '..', 'format.js'), 'utf8')}; return formatNtfy;`)();

const common = {
  schema_version: 1, client_id: 'client-a', client_name: 'Test client', bot_id: 'client-a:assistant',
  summary: 'Send the weekly recap email', operator_url: 'https://genesis.delo.sh/operator?client=client-a',
  occurred_at: '2026-11-16T12:00:00Z',
};
const env = (type, data) => ({ type, data: { ...common, ...data } });
const requested = env('bloodbank.genesis.approval.requested', {
  approval_id: 'prop-1', kind: 'email.send', question: 'Approve sending this recap? It keeps the group current.',
  channel: 'web', expires_at: '2026-11-17T12:00:00Z',
  action: { to: ['a@example.com'], cc: ['b@example.com'], subject: 'Recap', body_preview: 'Line one\nLine two' },
});

test('approval request is a priority 4 markdown push keyed on the approval id', () => {
  const n = formatNtfy(requested);
  assert.equal(n.topic, 'genesis');
  assert.equal(n.title, 'Approval needed · Test client');
  assert.equal(n.priority, 4);
  assert.equal(n.sequence_id, 'prop-1');
  assert.equal(n.markdown, true);
  assert.equal(n.click, common.operator_url);
  assert.deepEqual(n.actions, [{ action: 'view', label: 'Open operator', url: common.operator_url }]);
  for (const part of ['Send the weekly recap email', 'Approve sending this recap', '**To:** a@example.com',
    '**Cc:** b@example.com', '**Subject:** Recap', '> Line one', '> Line two', 'Expires ']) {
    assert.ok(n.message.includes(part), part);
  }
});

test('resolutions reuse the sequence id at priority 2', () => {
  for (const [t, word] of [['granted', 'Approved'], ['denied', 'Declined'], ['expired', 'Expired']]) {
    const n = formatNtfy(env(`bloodbank.genesis.approval.${t}`, { approval_id: 'prop-1', kind: 'email.send', status: t, decided_via: 'web' }));
    assert.equal(n.sequence_id, 'prop-1');
    assert.equal(n.priority, 2);
    assert.equal(n.title, `${word} · Test client`);
    assert.ok(n.message.startsWith(word));
  }
});

test('alerts: priority 4, 5 when critical, sequence id is the alert id', () => {
  const a = env('bloodbank.genesis.alert.raised', { alert_id: 'al-1', reason: 'operator_requested', severity: 'warning', detail: 'Desk asked for help' });
  const n = formatNtfy(a);
  assert.equal(n.title, 'Genesis alert · Test client');
  assert.equal(n.priority, 4);
  assert.equal(n.sequence_id, 'al-1');
  assert.ok(n.message.includes('operator requested') && n.message.includes('Desk asked for help'));
  assert.equal(formatNtfy(env('bloodbank.genesis.alert.raised', { ...a.data, severity: 'critical' })).priority, 5);
});

test('test events are labelled', () => {
  const n = formatNtfy({ ...requested, data: { ...requested.data, test: true } });
  assert.ok(n.title.startsWith('[test] '));
  assert.ok(n.tags.includes('test_tube'));
});

test('non-email actions fall back to a clipped JSON block; unknown types throw', () => {
  const n = formatNtfy(env('bloodbank.genesis.approval.requested', { approval_id: 'p', kind: 'calendar.create', question: 'q', channel: 'web', expires_at: '2026-11-17T12:00:00Z', action: { title: 'Lunch' } }));
  assert.ok(n.message.includes('"title": "Lunch"'));
  assert.throws(() => formatNtfy({ type: 'bloodbank.genesis.nope', data: common }));
});

test('workflow: no clock nodes, all five types bound, credential by id only', () => {
  assert.equal(EVENTS.length, 5);
  assert.deepEqual(workflow.nodes[0].parameters.events, EVENTS);
  assert.equal(workflow.nodes[0].parameters.delivery, 'durable');
  for (const node of workflow.nodes) {
    assert.ok(!/schedule|cron|interval/i.test(node.type), node.type);
  }
  const json = JSON.stringify(workflow);
  assert.ok(!/Bearer|tk_[a-z0-9]{10}/i.test(json));
  assert.equal(workflow.nodes[2].credentials.httpHeaderAuth.id, 'Ek5QjwyMWTolNgcO');
});
