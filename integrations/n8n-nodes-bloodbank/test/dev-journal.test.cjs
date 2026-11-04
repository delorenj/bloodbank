const { test } = require('node:test');
const assert = require('node:assert/strict');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { DatabaseSync } = require('node:sqlite');
const { DevJournal } = require('../src/nodes/DevJournal/DevJournal.node.ts');
const { JournalStore, journalStore } = require('../src/nodes/DevJournal/JournalStore.ts');

test('custom Dev Journal node persists verified input in the local SQLite store', async (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'dev-journal-node-'));
  const path = join(directory, 'state.sqlite');
  process.env.DEV_JOURNAL_DB_PATH = path;
  t.after(() => {
    journalStore().close();
    delete process.env.DEV_JOURNAL_DB_PATH;
    rmSync(directory, { recursive: true, force: true });
  });
  const reportDate = '2026-09-23';
  const event = {
    id: 'event-1', type: 'bloodbank.reporting.report.completed',
    producer: 'delonet-daily-report', data: {
      report_date: reportDate, run_id: 'run-1',
      content: { generation_id: 'gen-1', content_sha256: 'a'.repeat(64),
        markdown: '# Journal', report: { report_date: reportDate, run_id: 'run-1' },
        collector_facts: [] },
    },
  };
  const context = {
    getNodeParameter: (name) => name === 'operation' ? 'ingest' : undefined,
    getNode: () => ({ name: 'Persist verified snapshot', type: 'n8n-nodes-bloodbank.devJournal' }),
    getInputData: () => [{ json: event }],
    helpers: {
      async httpRequest() { throw new Error('unused'); },
      async httpRequestWithAuthentication() { throw new Error('unused'); },
    },
  };
  const output = await new DevJournal().execute.call(context);
  const rows = journalStore().rows('reports');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].report_key, `${reportDate}:gen-1`);
  assert.equal(output[0][0].json.receipt.source_event_id, 'event-1');
  const replay = await new DevJournal().execute.call(context);
  assert.equal(journalStore().rows('reports').length, 1);
  assert.equal(replay[0][0].json.already_received, true);
});

test('custom node runtime uses atomic reservation and ownership-safe settlement helpers', async (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'dev-journal-runtime-mail-'));
  const path = join(directory, 'state.sqlite');
  process.env.DEV_JOURNAL_DB_PATH = path;
  t.after(() => { journalStore().close(); delete process.env.DEV_JOURNAL_DB_PATH;
    rmSync(directory, { recursive: true, force: true }); });
  const key = '2026-09-23:runtime';
  journalStore().upsert('reports', 'report_key', key, { report_key: key,
    report_date: '2026-09-23', content_sha256: 'digest', status: 'complete', errors: '[]' });
  const node = new DevJournal();
  const invoke = (operation, input, mail) => node.execute.call({
    getNodeParameter: (name) => name === 'operation' ? operation : mail,
    getNode: () => ({ name: 'Mail test', type: 'n8n-nodes-bloodbank.devJournal' }),
    getInputData: () => [{ json: input }], helpers: {
      async httpRequest() { throw new Error('network forbidden'); },
      async httpRequestWithAuthentication() { throw new Error('network forbidden'); },
    },
  });
  const input = { report_key: key, report_date: '2026-09-23', status: 'complete', final_markdown: '# Done' };
  const mail = (await invoke('prepareEmail', input))[0][0].json;
  const failed = (await invoke('recordEmail', { error: 'timeout' }, mail))[0][0].json;
  assert.equal(failed.candybar.status, 'fail');
  assert.equal(journalStore().one('reports', 'report_key', key).pending_email, JSON.stringify(mail));
  const accepted = (await invoke('recordEmail', { id: 'accepted-runtime' }, mail))[0][0].json;
  assert.equal(accepted.candybar.status, 'success');
  assert.deepEqual(await invoke('prepareEmail', input), [[]]);
});

test('SQLite journal tables preserve keyed merges and indexed query fields', (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'dev-journal-store-'));
  const path = join(directory, 'state.sqlite');
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const store = new JournalStore(path);
  store.upsert('occurrences', 'occurrence_id', 'occ-1', {
    occurrence_id: 'occ-1', fingerprint: 'fp-1', report_date: '2026-09-23',
    summary: 'First observation', event_sent: false,
  });
  store.upsert('occurrences', 'occurrence_id', 'occ-1', {
    occurrence_id: 'occ-1', ticket_key: 'INFR-10', event_sent: true,
  });
  assert.equal(store.rows('occurrences').length, 1);
  assert.deepEqual(store.one('occurrences', 'occurrence_id', 'occ-1'), {
    occurrence_id: 'occ-1', fingerprint: 'fp-1', report_date: '2026-09-23',
    project_id: null, area: null, failure_mode: null, status: null, severity: null,
    summary: 'First observation', evidence: null, source_event_id: null,
    ticket_key: 'INFR-10', observed_at: null, event_sent: true,
  });
  assert.throws(() => store.upsert('occurrences', 'occurrence_id', 'occ-1', {
    occurrence_id: 'different', summary: 'bad',
  }), /Invalid occurrences key/);
  store.close();
  const reopened = new JournalStore(path);
  assert.equal(reopened.one('occurrences', 'occurrence_id', 'occ-1').ticket_key, 'INFR-10');
  reopened.close();
  const db = new DatabaseSync(path);
  const tables = db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    .all().map((row) => row.name);
  assert.deepEqual(tables, ['findings', 'occurrences', 'reports', 'rollups']);
  const indexes = db.prepare("SELECT name FROM sqlite_master WHERE type = 'index'")
    .all().map((row) => row.name);
  assert.ok(indexes.includes('reports_by_date'));
  assert.ok(indexes.includes('occurrences_by_fingerprint'));
  db.close();
});

test('legacy receipt migration is additive, infers phases and survives reopen', (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'dev-journal-legacy-'));
  const path = join(directory, 'state.sqlite');
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const db = new DatabaseSync(path);
  db.exec(`CREATE TABLE reports (report_key TEXT PRIMARY KEY, report_date TEXT,
    status TEXT, email_id TEXT, email_hash TEXT, payload TEXT);
    INSERT INTO reports VALUES ('partial', '2026-10-24', 'partial', 'id1', 'bff04773', 'keep');
    INSERT INTO reports VALUES ('complete', '2026-10-23', 'complete', 'id2', 'oldhash', 'keep2');`);
  db.close();
  const store = new JournalStore(path);
  assert.equal(store.one('reports', 'report_key', 'partial').email_phase, 'partial');
  assert.equal(store.one('reports', 'report_key', 'complete').email_phase, 'complete');
  assert.equal(store.one('reports', 'report_key', 'partial').email_hash, 'bff04773');
  assert.equal(store.one('reports', 'report_key', 'complete').payload, 'keep2');
  const mail = { report_key: 'partial', email_phase: 'complete',
    mail_key: 'dev-journal-v2:partial::complete', email_hash: 'dev-journal-v2:partial::complete',
    email: { text: 'frozen' } };
  store.reserveMail('partial', mail);
  store.close();
  const reopened = new JournalStore(path);
  assert.equal(reopened.one('reports', 'report_key', 'partial').pending_email, JSON.stringify(mail));
  assert.equal(reopened.reserveMail('partial', null).email.text, 'frozen');
  assert.equal(reopened.rows('reports').length, 2);
  assert.equal(reopened.reserveMail('complete', { ...mail, report_key: 'complete',
    mail_key: 'dev-journal-v2:complete::complete', email_hash: 'dev-journal-v2:complete::complete' }), null);
  reopened.close();
});
