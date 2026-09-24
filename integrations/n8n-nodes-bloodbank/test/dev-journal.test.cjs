const { test } = require('node:test');
const assert = require('node:assert/strict');
const { DevJournal } = require('../src/nodes/DevJournal/DevJournal.node.ts');

test('custom Dev Journal node reaches the in-process Data Table proxy', async () => {
  const rows = [];
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
      async getDataTableAggregateProxy() {
        return { async getManyAndCount() { return { data: [{ id: 'table-1', name: 'dev_journal_reports' }] }; } };
      },
      async getDataTableProxy() {
        return {
          async getManyRowsAndCount() { return { data: rows, count: rows.length }; },
          async upsertRow({ data }) { rows.push(data); return [data]; },
        };
      },
      async httpRequest() { throw new Error('unused'); },
      async httpRequestWithAuthentication() { throw new Error('unused'); },
    },
  };
  const output = await new DevJournal().execute.call(context);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].report_key, `${reportDate}:gen-1`);
  assert.equal(output[0][0].json.receipt.source_event_id, 'event-1');
});
