'use strict';

// Ticket Pickup Chip (wWXgCZiiIBWaRRzE), as exported to delonet-n8n-workflows.
//
// The hourly Stale Chip Sweep re-reads every ticket whose latest gateway turn
// ended in the last 48h (~90). Until 2026-11-06 it sent all of them to Plane
// at once, three GETs each; Plane's Postgres answered with HTTP 500 "too many
// clients" for 4-6 of them, and the first one threw in Chip — Plan Write and
// dropped every other ticket's write: the sweep failed every hour and fixed
// nothing. These tests pin the repair against the live export: Plane calls are
// throttled, chip-free tickets stop before the activity read, a transient
// failure on a swept ticket skips that ticket only, and the lane reports
// through Lifecycle Lane Errors.
//
// Set N8N_WORKFLOWS_DIR to point at another checkout of the export; without one
// the tests skip.

const assert = require('node:assert/strict');
const { existsSync, readdirSync, readFileSync } = require('node:fs');
const { homedir } = require('node:os');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const NAME = 'Ticket Pickup Chip';
const ERROR_WORKFLOW = '72y5JKBSJqYIjIEv';
const LABEL_ID = 'label-agent-working';
const BOARD = '7485039f-85b0-47db-ad99-d11497f043d7';

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
  const workflow = published || draft;
  return workflow ? { workflow } : { reason: `no export of "${NAME}" in ${dir}` };
}

const located = locateExport();
const WORKFLOW = located.workflow;
const skip = WORKFLOW ? false : located.reason;

const node = (name) => {
  const found = WORKFLOW.nodes.find((candidate) => candidate.name === name);
  assert.ok(found, `the chip lane has a "${name}" node`);
  return found;
};
const next = (from) => (WORKFLOW.connections[from]?.main?.[0] || []).map((edge) => edge.node);

function runCode(name, input, references = {}) {
  const sandbox = {
    $input: { all: () => input, first: () => input[0] },
    $: (ref) => {
      const items = references[ref];
      if (!items) throw new Error(`Code node referenced unknown node "${ref}"`);
      return { all: () => items, first: () => items[0], itemMatching: (index) => items[index] };
    },
    $env: {},
  };
  const script = new vm.Script(`(function () {\n${node(name).parameters.jsCode}\n})()`);
  return JSON.parse(JSON.stringify(script.runInNewContext(sandbox)));
}

// The exact item n8n's HTTP node (On Error = Continue) emitted for Plane's
// "too many clients" 500 in sweep execution 669070.
const PLANE_500 = Object.freeze({
  json: {
    error: {
      message: '500 - "{\\"error\\":\\"Something went wrong please try again later\\"}"',
      name: 'AxiosError',
      code: 'ERR_BAD_RESPONSE',
      status: 500,
    },
  },
});
const PLANE_401 = Object.freeze({ json: { error: { message: '401 - "{\\"detail\\":\\"Invalid token\\"}"', status: 401 } } });
const PLANE_404 = Object.freeze({ json: { error: { message: '404 - "Not found"', status: 404 } } });

const target = (n, extra = {}) => ({
  json: {
    action: 'remove',
    ws: 'transcription-queue',
    board: BOARD,
    ticketId: `ticket-${n}`,
    ticketKey: `HEYMA-${n}`,
    sweep: true,
    eventTime: '2026-11-06T10:00:00.000Z',
    ...extra,
  },
});
const resolved = (n, extra) => ({ json: { ...target(n, extra).json, labelId: LABEL_ID } });
const labelsBody = { json: { results: [{ id: LABEL_ID, name: 'agent:working' }] } };
const issue = (labels, extra = {}) => ({
  json: { labels, state: { id: 'state-todo', name: 'Todo' }, updated_at: '2026-11-06T10:00:01.000Z', ...extra },
});

test('the Plane reads are throttled and chip-free tickets stop before the activity read', { skip }, () => {
  assert.deepEqual(next('Chip — Read Issue'), ['Chip — Needs Write']);
  assert.deepEqual(next('Chip — Needs Write'), ['Chip — Read Activity']);
  assert.deepEqual(next('Chip — Read Activity'), ['Chip — Plan Write']);
  for (const name of ['Chip — List Labels', 'Chip — Read Issue', 'Chip — Read Activity', 'Chip — Write Labels']) {
    const http = node(name);
    assert.equal(http.onError, 'continueRegularOutput', `${name} hands failures to the next Code node`);
    const batch = http.parameters.options?.batching?.batch;
    // n8n fires every item of an HTTP node at once; batching spaces their start.
    assert.ok(batch, `${name} batches its requests`);
    assert.ok(batch.batchSize >= 1 && batch.batchSize <= 5, `${name} starts at most 5 requests per batch`);
    assert.ok(batch.batchInterval >= 100, `${name} pauses between batches`);
  }
});

test('the lane reports failures through Lifecycle Lane Errors', { skip }, () => {
  assert.equal(WORKFLOW.settings?.errorWorkflow, ERROR_WORKFLOW);
});

test('a transient Plane failure on a swept ticket skips that ticket and keeps the rest', { skip }, () => {
  const targets = Array.from({ length: 10 }, (_, n) => target(n));
  const bodies = targets.map((_, n) => (n === 3 ? PLANE_500 : labelsBody));
  const out = runCode('Chip — Resolve Label', bodies, { 'Chip — Target': targets });
  assert.equal(out.length, 9);
  assert.ok(!out.some((item) => item.json.ticketId === 'ticket-3'));

  const resolvedItems = targets.map((_, n) => resolved(n));
  const issues = resolvedItems.map((_, n) => (n === 5 ? PLANE_500 : issue([LABEL_ID])));
  const kept = runCode('Chip — Needs Write', issues, { 'Chip — Resolve Label': resolvedItems });
  assert.equal(kept.length, 9);

  const activity = resolvedItems.map((_, n) => (n === 7 ? PLANE_500 : { json: { results: [] } }));
  const writes = runCode('Chip — Plan Write', activity, {
    'Chip — Resolve Label': resolvedItems,
    'Chip — Read Issue': resolvedItems.map(() => issue([LABEL_ID])),
  });
  assert.equal(writes.length, 9, 'every other swept ticket still gets its chip removed');
  assert.ok(writes.every((item) => item.json.labels.length === 0));
});

test('a transient failure on a live turn still fails the run so it is reported', { skip }, () => {
  const live = [target(1, { sweep: false })];
  assert.throws(() => runCode('Chip — Resolve Label', [PLANE_500], { 'Chip — Target': live }), /HTTP 500/);
  const liveResolved = [resolved(1, { sweep: false })];
  assert.throws(() => runCode('Chip — Needs Write', [PLANE_500], { 'Chip — Resolve Label': liveResolved }), /HTTP 500/);
  assert.throws(
    () =>
      runCode('Chip — Plan Write', [PLANE_500], {
        'Chip — Resolve Label': liveResolved,
        'Chip — Read Issue': [issue([LABEL_ID])],
      }),
    /Read Activity failed .* HTTP 500/,
  );
});

test('a sweep that is mostly failing fails, because Plane itself is down', { skip }, () => {
  const targets = Array.from({ length: 6 }, (_, n) => target(n));
  const bodies = targets.map((_, n) => (n < 3 ? PLANE_500 : labelsBody));
  assert.throws(() => runCode('Chip — Resolve Label', bodies, { 'Chip — Target': targets }), /3 of 6 swept tickets/);
});

test('a non-transient failure still fails the sweep, and a deleted ticket is skipped', { skip }, () => {
  const targets = [target(1), target(2)];
  assert.throws(() => runCode('Chip — Resolve Label', [PLANE_401, labelsBody], { 'Chip — Target': targets }), /HTTP 401/);
  const resolvedItems = [resolved(1), resolved(2)];
  const kept = runCode('Chip — Needs Write', [PLANE_404, issue([LABEL_ID])], { 'Chip — Resolve Label': resolvedItems });
  assert.deepEqual(kept.map((item) => item.json.ticketId), ['ticket-2']);
});

test('Needs Write passes only tickets whose chip state the action would change', { skip }, () => {
  const items = [
    resolved(1, { action: 'remove' }), // chip present: may come off
    resolved(2, { action: 'remove' }), // chip already gone: no-op
    resolved(3, { action: 'add', sweep: false }), // chip absent: goes on
    resolved(4, { action: 'add', sweep: false }), // chip already on: no-op
    resolved(5, { action: 'remove' }), // soft-deleted ticket
  ];
  const issues = [
    issue([LABEL_ID, 'other']),
    issue(['other']),
    issue(['other']),
    issue([{ id: LABEL_ID }]),
    issue([LABEL_ID], { deleted_at: '2026-11-06T09:00:00Z' }),
  ];
  const kept = runCode('Chip — Needs Write', issues, { 'Chip — Resolve Label': items });
  assert.deepEqual(kept.map((item) => item.json.ticketId), ['ticket-1', 'ticket-3']);
});

test('a connection error on a swept ticket is transient, not "HTTP 127"', { skip }, () => {
  // n8n's own axios error for a refused connection; plane.delo.sh resolves to
  // 127.0.0.1 on this host, so the digits of the address must not read as a status.
  const refused = { json: { error: { message: 'connect ECONNREFUSED 127.0.0.1:443', code: 'ECONNREFUSED' } } };
  const timedOut = { json: { error: { message: 'connect ETIMEDOUT 192.168.1.20:443', code: 'ETIMEDOUT' } } };
  const targets = Array.from({ length: 10 }, (_, n) => target(n));
  const bodies = targets.map((_, n) => (n === 2 ? refused : n === 6 ? timedOut : labelsBody));
  const out = runCode('Chip — Resolve Label', bodies, { 'Chip — Target': targets });
  assert.equal(out.length, 8);
  // A live turn still reports it, with no invented status.
  assert.throws(
    () => runCode('Chip — Resolve Label', [refused], { 'Chip — Target': [target(1, { sweep: false })] }),
    /HTTP \? connect ECONNREFUSED/,
  );
});

test('a body that merely contains digits is not a status; n8n\'s "<status> - " prefix is', { skip }, () => {
  const resolvedItems = [resolved(1), resolved(2)];
  const odd = { json: { error: { message: 'request to board 404 workers failed' } } }; // no status: transient on a sweep
  const gone = { json: { error: { message: '404 - "Not found"' } } }; // status from the prefix: deleted, skipped
  const kept = runCode('Chip — Needs Write', [odd, gone], { 'Chip — Resolve Label': resolvedItems });
  assert.deepEqual(kept, []);
  assert.throws(
    () => runCode('Chip — Needs Write', [odd], { 'Chip — Resolve Label': [resolved(1, { sweep: false })] }),
    /HTTP \?/,
  );
});
