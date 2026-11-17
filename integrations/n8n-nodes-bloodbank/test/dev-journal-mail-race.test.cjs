const { test } = require('node:test');
const assert = require('node:assert/strict');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join, resolve } = require('node:path');
const { Worker } = require('node:worker_threads');
const { JournalStore } = require('../src/nodes/DevJournal/JournalStore.ts');
const { journalPrograms } = require('../src/nodes/DevJournal/programs.generated.ts');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const key = '2026-09-23:gen';
const RealDate = Date;
class FixedDate extends RealDate {
  constructor(...args) { super(...(args.length ? args : ['2026-09-24T11:00:00Z'])); }
  static now() { return new RealDate('2026-09-24T11:00:00Z').getTime(); }
}
function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'journal-mail-race-'));
  const path = join(directory, 'state.sqlite');
  const stores = [new JournalStore(path), new JournalStore(path)];
  stores[0].upsert('reports', 'report_key', key, { report_key: key,
    report_date: '2026-09-23', generation_id: 'gen', content_sha256: 'abc',
    status: 'complete', errors: '[]', payload: JSON.stringify({ markdown: '# Original', report: {} }) });
  t.after(() => { stores.forEach((s) => s.close()); rmSync(directory, { recursive: true, force: true }); });
  return { path, stores };
}
function helpers(store) {
  return { journalRows: (kind) => store.rows(kind), journalOne: (...args) => store.one(...args),
    journalUpsert: (...args) => store.upsert(...args),
    journalReserveMail: (...args) => store.reserveMail(...args),
    journalSettleMail: (...args) => store.settleMail(...args),
    httpRequest: async () => ({ note_id: 'mocked-note' }) };
}
function run(operation, h, input = {}, ctx = {}) {
  return new AsyncFunction('helpers', '$input', 'ctx', 'Date', journalPrograms[operation])(
    h, { first: () => ({ json: input }), all: () => [{ json: input }] }, ctx, FixedDate);
}
function candidate(phase, text = phase) {
  const identity = `dev-journal-v2:${key}:abc:${phase}`;
  return { report_key: key, email_phase: phase, mail_key: identity, email_hash: identity, email: { text } };
}

for (const processorPhase of ['partial', 'complete']) {
  for (const winner of ['deadline', 'processor']) {
    test(`real deadline/processor ${processorPhase} race: ${winner} reserves first after both stale reads`, async (t) => {
      const { stores } = fixture(t);
      let ready;
      const bothReady = new Promise((resolve) => { ready = resolve; });
      const waiting = {};
      const gated = (store, lane) => {
        const h = helpers(store);
        h.journalReserveMail = async (k, mail) => {
          if (mail) {
            await new Promise((resolve) => { waiting[lane] = resolve; if (Object.keys(waiting).length === 2) ready(); });
          }
          return store.reserveMail(k, mail);
        };
        return h;
      };
      const input = { report_key: key, report_date: '2026-09-23', status: processorPhase, final_markdown: '# Processor' };
      const deadline = run('deadlineMail', gated(stores[0], 'deadline'));
      const processor = run('prepareEmail', gated(stores[1], 'processor'), input);
      await bothReady;
      waiting[winner]();
      const first = await (winner === 'deadline' ? deadline : processor);
      waiting[winner === 'deadline' ? 'processor' : 'deadline']();
      const [d, p] = await Promise.all([deadline, processor]);
      assert.deepEqual(d, p, 'both lanes must transport the persisted winner, never their own stale body');
      assert.deepEqual(JSON.parse(stores[0].one('reports', 'report_key', key).pending_email), first[0].json);
      const mail = first[0].json;
      assert.equal(mail.email_phase, winner === 'deadline' ? 'partial' : processorPhase);
      assert.equal((await run('recordEmail', helpers(stores[0]), { id: 'winner-id' }, { mail }))[0].json.delivered, true);
      if (mail.email_phase === 'partial') {
        const completion = (await run('prepareEmail', helpers(stores[1]), { ...input, status: 'complete' }))[0].json;
        const before = stores[0].one('reports', 'report_key', key);
        assert.equal(stores[0].settleMail(mail, 'winner-id'), true, 'known duplicate receipt is an idempotent no-op');
        assert.deepEqual(stores[0].one('reports', 'report_key', key), before);
        for (const response of [{ id: 'late-partial-id' }, { error: 'late timeout' }]) {
          const late = (await run('recordEmail', helpers(stores[0]), response, { mail }))[0].json;
          assert.equal(late.stale, true);
          assert.equal(late.candybar.status, 'fail');
          assert.deepEqual(stores[0].one('reports', 'report_key', key), before);
        }
        assert.equal(stores[1].settleMail(completion, 'complete-id'), true);
        assert.deepEqual(JSON.parse(stores[0].one('reports', 'report_key', key).email_receipts).map((r) => r.id),
          ['winner-id', 'complete-id']);
      }
      assert.deepEqual(await run('prepareEmail', helpers(stores[0]), { ...input, status: 'partial' }), []);
      assert.deepEqual(await run('prepareEmail', helpers(stores[1]), { ...input, status: 'complete' }), []);
      assert.equal(stores[0].one('reports', 'report_key', key).status, 'complete', 'deadline must not regress processing');
    });
  }
}

for (const firstPhase of ['partial', 'complete']) {
  test(`receipt before stale competing reservation rechecks ${firstPhase} eligibility atomically`, async (t) => {
    const { stores } = fixture(t);
    const stalePartial = candidate('partial', 'stale deadline body');
    const staleComplete = candidate('complete', 'stale processor body');
    const first = stores[0].reserveMail(key, firstPhase === 'partial' ? stalePartial : staleComplete);
    assert.equal(stores[0].settleMail(first, 'first-id'), true);
    const other = stores[1].reserveMail(key, firstPhase === 'partial' ? staleComplete : stalePartial);
    if (firstPhase === 'complete') {
      assert.equal(other, null);
      assert.equal(stores[0].one('reports', 'report_key', key).pending_email, '');
    } else {
      assert.deepEqual(other, staleComplete);
      const before = stores[1].one('reports', 'report_key', key);
      assert.equal(stores[0].settleMail(first, null, 'late failure'), false);
      assert.deepEqual(stores[1].one('reports', 'report_key', key), before);
    }
  });
}

test('ambiguous provider retry survives close/reopen with exact frozen bytes and ownership', async (t) => {
  const { path, stores } = fixture(t);
  const mail = stores[0].reserveMail(key, candidate('partial', 'original bytes'));
  const failure = (await run('recordEmail', helpers(stores[0]), { error: 'timeout after accept' }, { mail }))[0].json;
  assert.equal(failure.candybar.status, 'fail');
  assert.equal(stores[0].one('reports', 'report_key', key).status, 'complete');
  stores[1].close();
  stores[1] = new JournalStore(path);
  assert.deepEqual(stores[1].reserveMail(key, candidate('complete', 'changed bytes')), mail);
  assert.equal(stores[1].settleMail({ ...mail, email_phase: 'complete' }, 'wrong'), false);
  assert.equal(stores[1].settleMail(mail, 'original-provider-id'), true);
  assert.equal(stores[0].reserveMail(key, candidate('partial', 'churn')), null);
  assert.equal(stores[0].reserveMail(key, candidate('complete')).email_phase, 'complete');
});

test('legacy pending reservation migrates without changing frozen payload, key or old receipt', (t) => {
  const { path, stores } = fixture(t);
  const mail = candidate('complete', 'legacy frozen bytes');
  stores.forEach((s) => s.close());
  const { DatabaseSync } = require('node:sqlite');
  const db = new DatabaseSync(path);
  db.exec('ALTER TABLE reports DROP COLUMN email_phase; ALTER TABLE reports DROP COLUMN email_receipts;');
  db.prepare("UPDATE reports SET status = 'partial', email_id = 'historical-id', email_hash = 'old-body-hash', pending_email = ?")
    .run(JSON.stringify(mail));
  db.close();
  stores[0] = new JournalStore(path); stores[1] = new JournalStore(path);
  assert.deepEqual(stores[0].reserveMail(key, candidate('partial', 'churn')), mail);
  assert.equal(stores[1].settleMail(mail, 'completion-id'), true);
  const row = stores[0].one('reports', 'report_key', key);
  assert.deepEqual(JSON.parse(row.email_receipts).map((r) => [r.id, r.key, r.phase]), [
    ['historical-id', 'old-body-hash', 'partial'], ['completion-id', mail.mail_key, 'complete'],
  ]);
  assert.equal(row.email_phase, 'complete');
  assert.equal(stores[0].reserveMail(key, candidate('partial')), null);
});

test('generic upsert cannot bypass frozen reservation or mutate receipt history', (t) => {
  const { stores } = fixture(t);
  const mail = stores[0].reserveMail(key, candidate('partial'));
  for (const field of ['pending_email', 'email_phase', 'email_id', 'email_hash', 'email_sent_at', 'email_receipts']) {
    assert.throws(() => stores[1].upsert('reports', 'report_key', key, { [field]: '' }), /requires reserveMail/);
  }
  assert.deepEqual(stores[1].reserveMail(key, candidate('complete')), mail);
});

test('two SQLite connections in simultaneous worker threads reserve one durable payload/key', async (t) => {
  const { path, stores } = fixture(t);
  const modulePath = resolve(__dirname, '../dist/nodes/DevJournal/JournalStore.js');
  const gate = new SharedArrayBuffer(4);
  const workers = ['partial', 'complete'].map((phase) => new Worker(`
    const { parentPort, workerData } = require('node:worker_threads');
    const { JournalStore } = require(workerData.modulePath);
    const store = new JournalStore(workerData.path);
    parentPort.postMessage({ready:true});
    Atomics.wait(new Int32Array(workerData.gate), 0, 0);
    const mail = store.reserveMail(workerData.key, workerData.mail);
    store.close(); parentPort.postMessage({mail});
  `, { eval: true, workerData: { path, modulePath, gate, key, mail: candidate(phase) } }));
  t.after(async () => { await Promise.all(workers.map((w) => w.terminate())); });
  const result = workers.map((w) => new Promise((resolve, reject) => {
    w.on('message', (m) => { if (m.mail) resolve(m.mail); }); w.on('error', reject);
  }));
  await Promise.all(workers.map((w) => new Promise((resolve, reject) => {
    w.once('message', resolve); w.on('error', reject);
  })));
  Atomics.store(new Int32Array(gate), 0, 1); Atomics.notify(new Int32Array(gate), 0, 2);
  const [a, b] = await Promise.all(result);
  assert.deepEqual(a, b);
  assert.deepEqual(JSON.parse(stores[0].one('reports', 'report_key', key).pending_email), a);
});
