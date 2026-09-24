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
