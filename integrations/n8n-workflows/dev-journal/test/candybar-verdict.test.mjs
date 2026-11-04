import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const candybar = '/home/delorenj/code/33GOD/candybar/src';
const read = (file) => readFileSync(join(root, file), 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const workflow = (lane) => JSON.parse(read(`${lane}.workflow.json`));
const runCode = (node, items) => new AsyncFunction('helpers', '$input', 'ctx', node.parameters.jsCode)(
  {}, { all: () => items, first: () => items[0] }, {});
const verdicts = (bodies) => {
  const script = 'import json,sys\nfrom candybar.n8n_webhook import _verdict\nprint(json.dumps([_verdict(json.dumps(body)) for body in json.load(sys.stdin)]))';
  const result = spawnSync('python3', ['-c', script], {
    input: JSON.stringify(bodies), encoding: 'utf8',
    env: { ...process.env, PYTHONPATH: candybar },
  });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
};
const next = (wf, from, branch = 0) => wf.connections[from]?.main[branch]?.map((edge) => edge.node) || [];
const node = (wf, name) => wf.nodes.find((item) => item.name === name);

async function terminal(wf, start, items) {
  const route = node(wf, start);
  assert.equal(route.alwaysOutputData, true);
  const gateName = next(wf, start)[0];
  const gate = node(wf, gateName);
  assert.equal(gate.type, 'n8n-nodes-base.if');
  assert.equal(gate.parameters.conditions.conditions[0].operator.type, 'boolean');
  assert.equal(gate.parameters.conditions.conditions[0].operator.operation, 'true');
  const field = gate.parameters.conditions.conditions[0].leftValue.match(/\$json\.([a-z_]+)/)[1];
  const branch = items[0]?.json[field] ? 0 : 1;
  const targetName = next(wf, gateName, branch)[0];
  const target = node(wf, targetName);
  assert.ok(target, `${wf.name}: missing terminal for ${start} branch ${branch}`);
  if (branch === 1) {
    assert.equal(target.type, 'n8n-nodes-base.code');
    return (await runCode(target, items.length ? items : [{ json: {} }]))[0].json;
  }
  return { branch, targetName };
}

test('actual Candybar verdict parser sees failed provider, accepted provider and no-work', async () => {
  const wf = workflow('processor');
  const report = { report_key: '2026-10-24:g', report_date: '2026-10-24',
    errors: '[]', pending_email: 'frozen-payload-and-key' };
  const writes = [];
  const helpers = {
    journalOne: async () => report,
    journalSettleMail: async (mail, id, reason) => {
      if (report.pending_email !== JSON.stringify(mail)) return false;
      const update = id ? { pending_email: '', email_id: id } : { errors: JSON.stringify([reason]) };
      writes.push(update);
      Object.assign(report, update);
      return true;
    },
  };
  const record = node(wf, 'Store Resend receipt');
  const fn = new AsyncFunction('helpers', '$input', 'ctx', record.parameters.operation === 'recordEmail'
    ? `${read('src/common.js')}\n${read('src/record-email.js')}` : 'throw Error("wrong operation")');
  const mail = { report_key: report.report_key, email_hash: 'hash', email_phase: 'partial', mail_key: 'stable-key' };
  report.pending_email = JSON.stringify(mail);
  const frozen = report.pending_email;
  const failed = (await fn(helpers, { first: () => ({ json: { error: 'timeout' } }) }, { mail }))[0].json;
  assert.equal(failed.delivered, false);
  assert.equal(report.pending_email, frozen);
  assert.ok(writes.every((write) => !Object.hasOwn(write, 'pending_email')));
  const accepted = (await fn(helpers, { first: () => ({ json: { id: 'accepted' } }) }, { mail }))[0].json;
  assert.equal(accepted.delivered, true);
  assert.equal(report.pending_email, '');
  const idle = await terminal(wf, 'Claim one report and prepare extraction', []);
  const suppressed = await terminal(wf, 'Only live mail not yet sent', []);
  const actual = verdicts([failed, accepted, idle, suppressed]);
  assert.deepEqual(actual.map((value) => value.status), ['fail', 'success', 'success', 'success']);
  assert.match(actual[0].message, /Resend pending:.*timeout/);
});

test('all four generated webhook lanes terminate truthfully on empty and active paths', async () => {
  const cases = [
    ['processor', 'Claim one report and prepare extraction', 'report_key', 'Extract and classify findings'],
    ['processor', 'Only live mail not yet sent', 'mail_key', 'Send daily journal'],
    ['deadline', 'Prepare honest partial if pending', 'mail_key', 'Send pending daily journal'],
    ['incidents', 'Load unsent incidents', 'occurrence_id', 'Publish observed incident'],
    ['rollups', 'Upsert weekly and monthly Infra notes', 'closed_through', 'Rollup complete'],
  ];
  const idle = [], active = [];
  for (const [lane, start, field, target] of cases) {
    const wf = workflow(lane);
    assert.equal(node(wf, 'n8n-nodes-base.webhook'), undefined);
    const empty = await terminal(wf, start, []);
    idle.push(empty);
    assert.equal(empty.no_work, true);
    assert.equal((await terminal(wf, start, [{ json: { [field]: 'present' } }])).targetName, target);
    if (target === 'Rollup complete') {
      const result = await runCode(node(wf, target), [{ json: { closed_through: '2026-10-24' } }]);
      active.push(result[0].json);
    } else if (lane === 'incidents') {
      const finish = next(wf, 'Record incident publication')[0];
      assert.equal(finish, 'Incident publication complete');
      active.push((await runCode(node(wf, finish), [{ json: { event_sent: true } }]))[0].json);
    }
    assert.equal(wf.active, false);
    assert.equal(wf.nodes.find((n) => n.type === 'n8n-nodes-base.webhook').parameters.responseMode, 'lastNode');
  }
  assert.ok(verdicts([...idle, ...active]).every((value) => value.status === 'success'));
});
