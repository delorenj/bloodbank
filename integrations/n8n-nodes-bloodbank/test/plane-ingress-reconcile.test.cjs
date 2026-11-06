'use strict';

// Plane Ingress Reconcile (U4hYm3BYPPeZNDHQ), as exported to delonet-n8n-workflows.
//
// Candybar is the only clock: the candybar plane-ingress-reconcile
// (bloodbank/.candybars) POSTs the workflow's webhook at 3-59/10 and waits for
// the answer. The workflow carries no Schedule Trigger, and answers only after
// the sweep's recovery pages went out, with a verdict read from its Report.
//
// Set N8N_WORKFLOWS_DIR to point at another checkout of the export; without one
// the export tests skip.

const assert = require('node:assert/strict');
const { existsSync, readdirSync, readFileSync } = require('node:fs');
const { homedir } = require('node:os');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const NAME = 'Plane Ingress Reconcile';
const MANIFEST = join(__dirname, '..', '..', '..', '.candybars', 'plane-ingress-reconcile.candybar.toml');

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
  assert.ok(found, `the reconcile workflow has a "${name}" node`);
  return found;
};
const next = (from, output = 0) => (WORKFLOW.connections[from]?.main?.[output] || []).map((edge) => edge.node);

function verdict(report) {
  const sandbox = { $input: { all: () => [{ json: report }], first: () => ({ json: report }) } };
  const script = new vm.Script(`(function () {\n${node('Reconcile — Verdict').parameters.jsCode}\n})()`);
  return JSON.parse(JSON.stringify(script.runInNewContext(sandbox)))[0].json;
}

// The Report item of a real sweep (execution 671305), trimmed.
const CLEAN = Object.freeze({
  ok: true,
  operation: 'reconcile',
  routed_boards: 45,
  boards_checked: 41,
  in_window: 10,
  already_on_bus: 10,
  missing: 0,
  recovered: 0,
  recovered_tickets: [],
  failed: 0,
  deferred: 0,
  partial: false,
});

test('the manifest runs the workflow through Candybar on the old cadence', () => {
  const manifest = readFileSync(MANIFEST, 'utf8');
  assert.match(manifest, /^kind = "n8n"$/m);
  assert.match(manifest, /^schedule = "3-59\/10 \* \* \* \*"$/m);
  assert.match(manifest, /^webhook = "candybar\/plane-ingress-reconcile"$/m);
  assert.match(manifest, /^enabled = false$/m, 'enabled is never synced; the file stays disabled');
});

test('the workflow has no clock of its own: Candybar fires its webhook', { skip }, () => {
  assert.ok(!WORKFLOW.nodes.some((candidate) => /schedule|cron|interval/i.test(candidate.type)), 'no Schedule Trigger');
  const manifest = readFileSync(MANIFEST, 'utf8');
  const trigger = node('Candybar Tick');
  assert.equal(trigger.type, 'n8n-nodes-base.webhook');
  assert.equal(trigger.parameters.httpMethod, 'POST');
  assert.equal(trigger.parameters.path, manifest.match(/^webhook = "([^"]+)"$/m)[1]);
  // "Immediately" would make every run read as a success to Candybar.
  assert.equal(trigger.parameters.responseMode, 'responseNode');
  assert.deepEqual(next('Candybar Tick'), ['Reconcile Missed Tickets']);
});

test('Candybar is answered from the Report, after the recovery pages', { skip }, () => {
  assert.deepEqual(next('Reconcile Missed Tickets', 0), ['Recovered Ticket']);
  assert.deepEqual(next('Reconcile Missed Tickets', 1), ['Reconcile — Verdict']);
  assert.deepEqual(next('Reconcile — Verdict'), ['Answer Candybar']);
  assert.equal(node('Answer Candybar').type, 'n8n-nodes-base.respondToWebhook');
  // Execution order v1 runs the upper branch first.
  assert.equal(WORKFLOW.settings?.executionOrder, 'v1');
  assert.ok(node('Reconcile — Verdict').position[1] > node('Recovered Ticket').position[1]);
});

test('the verdict says what the sweep did, and fails a partial publish', { skip }, () => {
  const clean = verdict(CLEAN);
  assert.equal(clean.candybar.status, 'success');
  assert.equal(clean.candybar.message, 'reconcile: 41/45 boards, 10 in window, 0 recovered');

  const recovered = verdict({ ...CLEAN, recovered: 2, recovered_tickets: ['HEYMA-14', 'BB-9'] });
  assert.equal(recovered.candybar.status, 'success');
  assert.match(recovered.candybar.message, /2 recovered, \(HEYMA-14, BB-9\)/);

  const partial = verdict({
    ...CLEAN,
    ok: false,
    recovered: 1,
    recovered_tickets: ['BB-9'],
    failed: 1,
    failures: [{ ticket_key: 'HEYMA-14', stage: 'publish', reason: 'nats: timeout' }],
  });
  assert.equal(partial.candybar.status, 'fail');
  assert.match(partial.candybar.message, /1 failed: HEYMA-14 nats: timeout/);
});
