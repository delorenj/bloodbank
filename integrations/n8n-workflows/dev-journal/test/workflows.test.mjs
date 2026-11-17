import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const read = (file) => readFileSync(join(root, file), 'utf8');
const code = (name) => `${read('src/common.js')}\n${read(`src/${name}.js`)}`;
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
function store(names) {
  const data = new Map(names.map((name) => [`dev_journal_${name}`, []]));
  const calls = { http: [] };
  const helpers = {
    async journalRows(kind) {
      return data.get(`dev_journal_${kind}`) || [];
    },
    async journalOne(kind, key, value) {
      return data.get(`dev_journal_${kind}`)?.find((row) => row[key] === value) || null;
    },
    async journalUpsert(kind, key, value, update) {
      const rows = data.get(`dev_journal_${kind}`);
      if (!rows) throw new Error(`Unknown table ${kind}`);
      let row = rows.find((item) => item[key] === value);
      if (row) Object.assign(row, update);
      else { row = { ...update }; rows.push(row); }
      return row;
    },
    async journalReserveMail(key, mail) {
      const row = await helpers.journalOne('reports', 'report_key', key);
      if (row.pending_email) return JSON.parse(row.pending_email);
      if (!mail || (row.email_id && (mail.email_phase === 'partial' || row.email_phase === 'complete'))) return null;
      row.pending_email = JSON.stringify(mail);
      return mail;
    },
    async journalSettleMail(mail, id, reason) {
      const row = await helpers.journalOne('reports', 'report_key', mail.report_key);
      if (row.pending_email !== JSON.stringify(mail)) return false;
      if (id) Object.assign(row, { email_id: id, email_hash: mail.email_hash,
        email_phase: mail.email_phase, pending_email: '' });
      else Object.assign(row, { errors: JSON.stringify([reason]), next_attempt_at: new Date().toISOString() });
      return true;
    },
    async httpRequest(options) {
      calls.http.push(options);
      if (options.url.includes('/v1/registry')) return { projects: { infra: { ticket_provider: {
        type: 'plane', state: 'linked', workspace: '33god', board_id: 'infra-board', identifier: 'INFR',
      } } } };
      return { note_id: `note-${calls.http.length}` };
    },
  };
  return { data, calls, helpers };
}
async function execute(name, env, input, ctx = {}) {
  const fn = new AsyncFunction('helpers', '$input', 'ctx', code(name));
  return fn(env.helpers, { all: () => input.map((json) => ({ json })),
    first: () => ({ json: input[0] }) }, ctx);
}

test('generated workflows have valid graph, deterministic IDs, and compilable Code nodes', () => {
  const files = readdirSync(root).filter((name) => name.endsWith('.workflow.json'));
  assert.equal(files.length, 7);
  const ids = new Set();
  for (const file of files) {
    const wf = JSON.parse(read(file));
    assert.ok(wf.id && !ids.has(wf.id));
    ids.add(wf.id);
    assert.equal(wf.active, false);
    assert.equal(wf.settings.timezone, 'America/New_York');
    const names = new Set(wf.nodes.map((node) => node.name));
    assert.equal(names.size, wf.nodes.length);
    for (const [from, edges] of Object.entries(wf.connections)) {
      assert.ok(names.has(from));
      const targets = edges.main.flat().map((target) => target.node);
      assert.equal(new Set(targets).size, targets.length, `${file}: duplicate edge from ${from}`);
      for (const target of edges.main.flat()) assert.ok(names.has(target.node));
    }
    for (const node of wf.nodes.filter((node) => node.type === 'n8n-nodes-base.code')) {
      assert.doesNotThrow(() => new AsyncFunction('helpers', '$input', 'ctx', node.parameters.jsCode));
    }
    assert.ok(!read(file).includes('DEV_JOURNAL_BACKFILL_AUTH_CREDENTIAL_ID'));
    assert.ok(!read(file).includes('openrouter.ai/api/v1/chat/completions'));
  }
  const incidents = JSON.parse(read('incidents.workflow.json'));
  assert.ok(incidents.nodes.some((node) => node.type === 'n8n-nodes-base.manualTrigger'));
});

test('ingress stores one immutable generation before returning its Bloodbank receipt', async () => {
  const env = store(['reports']);
  const date = '2026-09-23';
  const content = { generation_id: 'generation-1', content_sha256: 'a'.repeat(64),
    markdown: '# Journal', report: { report_date: date, run_id: 'run-1' }, collector_facts: [] };
  const event = { id: 'evt-1', type: 'bloodbank.reporting.report.completed',
    producer: 'delonet-daily-report', data: { report_date: date, run_id: 'run-1', content } };
  const first = await execute('ingest', env, [event]);
  assert.equal(first[0].json.receipt.source_event_id, 'evt-1');
  assert.equal(env.data.get('dev_journal_reports').length, 1);
  env.data.get('dev_journal_reports')[0].status = 'complete';
  const replay = await execute('ingest', env, [event]);
  assert.equal(replay[0].json.already_received, true);
  assert.equal(env.data.get('dev_journal_reports')[0].status, 'complete');
  const changed = structuredClone(event);
  changed.data.content.content_sha256 = 'b'.repeat(64);
  await assert.rejects(() => execute('ingest', env, [changed]), /Immutable report generation changed/);
});

test('extractor receives full report and covers Needs you plus collector caveats', async () => {
  const env = store(['reports', 'findings']);
  const date = '2026-09-23';
  const content = { markdown: 'FULL JOURNAL BODY',
    report: { sections: [{ id: 'summary', body: '## Needs you\n- Broken broker\n- Missing report\n## What happened' }] },
    collector_facts: [{ id: 'delivery', status: 'partial', summary: 'Two missing', caveats: ['Gap on Tuesday'] }] };
  env.data.get('dev_journal_reports').push({ report_key: `${date}:gen`, report_date: date,
    run_id: 'run', status: 'pending', backfill: false, payload: JSON.stringify(content), attempts: 0 });
  env.data.get('dev_journal_findings').push({ fingerprint: 'infra:report-delivery:old-wording',
    project_id: 'infra', area: 'report delivery', failure_mode: 'missing published reports',
    summary: 'Report archive has missing days', status: 'open', last_seen: '2026-09-22' });
  const out = await execute('select-report', env, [{}]);
  assert.deepEqual(out[0].json.source_candidates.map((source) => source.id), [
    'needs-you:1', 'needs-you:2', 'collector:delivery:status', 'collector:delivery:caveat:1',
  ]);
  assert.ok(out[0].json.llm_request.messages[1].content.includes('FULL JOURNAL BODY'));
  assert.equal(JSON.parse(out[0].json.llm_request.messages[1].content).known_findings[0].fingerprint,
    'infra:report-delivery:old-wording');
  assert.match(out[0].json.llm_request.messages[0].content, /existing_fingerprint/);
  assert.equal(out[0].json.llm_request.model, 'automaticai/auto/fast');
  assert.equal(env.data.get('dev_journal_reports')[0].status, 'processing');
});

test('semantic recurrence uses one existing issue across report wording and project aliases', async () => {
  const env = store(['reports', 'findings', 'occurrences', 'rollups']);
  const anchor = 'infra:report-delivery:missing-valid-published-report-for-one-due-day';
  env.data.get('dev_journal_findings').push({ fingerprint: anchor, project_id: 'infra',
    area: 'report delivery', failure_mode: 'Missing valid published report for one due day',
    summary: 'Report delivery is degraded because a due day has no valid published report',
    status: 'open', ticket_id: 'issue-5', ticket_key: 'INFR-5', active: true,
    first_seen: '2026-09-23', last_seen: '2026-09-23' });
  for (const [date, project, area, failure, summary, reference] of [
    ['2026-08-18', 'infra', 'report-delivery', 'Missing and invalid daily reports with false success claims',
      'Report delivery degraded with five days missing and one invalid', anchor],
    ['2026-08-19', 'delonet-daily-report', 'report delivery', 'Archive has no valid published report',
      'Nightly report delivery has missing and invalid days', ''],
  ]) {
    const reportKey = `${date}:gen`;
    env.data.get('dev_journal_reports').push({ report_key: reportKey, report_date: date,
      source_event_id: `evt-${date}`, run_id: `run-${date}`, generation_id: 'gen',
      status: 'processing', backfill: true, payload: JSON.stringify({ markdown: '# Journal',
        report: { report_date: date }, collector_facts: [] }), errors: '[]',
      received_at: `${date}T06:00:00Z` });
    const llm = { choices: [{ message: { content: JSON.stringify({ findings: [{
      project_id: project, area, failure_mode: failure, summary, status: 'open',
      severity: 'high', evidence: [summary], source_ids: ['needs-you:1'],
      ...(reference ? { existing_fingerprint: reference } : {}),
    }], non_issues: [] }) } }] };
    const result = await execute('process-report', env, [llm], { selected: {
      report_key: reportKey, source_candidates: [{ id: 'needs-you:1', text: summary }],
    } });
    assert.equal(result[0].json.status, 'complete');
  }
  const occurrences = env.data.get('dev_journal_occurrences');
  assert.equal(occurrences.length, 2);
  assert.deepEqual(new Set(occurrences.map((row) => row.fingerprint)), new Set([anchor]));
  assert.ok(occurrences.every((row) => row.ticket_key === 'INFR-5'));
  assert.equal(env.data.get('dev_journal_findings').length, 1);
  const RealDate = Date;
  const FixedDate = class extends RealDate {
    constructor(...args) { super(...(args.length ? args : ['2026-09-08T12:00:00Z'])); }
  };
  const fn = new AsyncFunction('helpers', '$input', 'ctx', 'Date', code('rollup'));
  await fn(env.helpers, { all: () => [{ json: {} }], first: () => ({ json: {} }) }, {}, FixedDate);
  const monthly = env.data.get('dev_journal_rollups').find((row) => row.period_key === 'monthly:2026-08');
  assert.match(monthly.content, /infra:report-delivery:missing-or-invalid-report: 2 day\(s\)/);
});

test('backfill finalizer links every historical occurrence to its one Plane ticket', async () => {
  const env = store(['reports', 'findings', 'occurrences']);
  const fingerprint = 'infra:report-delivery:missing-or-invalid-report';
  env.data.get('dev_journal_reports').push({ backfill: true, status: 'complete' });
  env.data.get('dev_journal_findings').push({ fingerprint, project_id: 'infra',
    area: 'report-delivery', failure_mode: 'missing-or-invalid-report',
    summary: 'Daily reports are missing', severity: 'high', status: 'open',
    first_seen: '2026-08-18', last_seen: '2026-08-19', last_occurrence_id: 'occ-2' });
  env.data.get('dev_journal_occurrences').push(
    { occurrence_id: 'occ-1', fingerprint, evidence: '["A missing report"]', ticket_key: '' },
    { occurrence_id: 'occ-2', fingerprint, evidence: '["An invalid report"]', ticket_key: '' },
    { occurrence_id: 'unrelated', fingerprint: 'other', ticket_key: '' },
  );
  let ticketCreates = 0;
  env.helpers.httpRequestWithAuthentication = async (_type, request) => {
    if (request.url.endsWith('/states/')) return { results: [
      { id: 'backlog', name: 'Backlog', group: 'backlog' },
    ] };
    if (request.url.endsWith('/issues/') && request.method === 'GET') return { results: [] };
    if (request.url.endsWith('/issues/') && request.method === 'POST') {
      ticketCreates++;
      return { id: 'issue-7', sequence_id: 7 };
    }
    throw new Error(`Unexpected Plane ${request.method} ${request.url}`);
  };
  const result = await execute('backfill-finalize', env, [{}]);
  assert.equal(result[0].json.created, 1);
  assert.equal(ticketCreates, 1);
  assert.deepEqual(env.data.get('dev_journal_occurrences').map((row) => row.ticket_key),
    ['INFR-7', 'INFR-7', '']);
  assert.equal((await execute('backfill-finalize', env, [{}]))[0].json.created, 0);
  assert.equal(ticketCreates, 1);
});

test('recurrence references require a known, related fingerprint', () => {
  const helpers = new Function(`${read('src/common.js')}\nreturn { semanticCategory, validRecurrenceReference };`)();
  const report = { project_id: 'delonet-daily-report', area: 'report delivery',
    failure_mode: 'Archive missing a published report', summary: 'Report delivery degraded' };
  const prior = { project_id: 'infra', area: 'report-delivery',
    failure_mode: 'Missing valid daily report', summary: 'Report archive has missing days' };
  assert.equal(helpers.semanticCategory(report), 'infra:report-delivery:missing-or-invalid-report');
  assert.equal(helpers.validRecurrenceReference(report, prior), true);
  assert.equal(helpers.validRecurrenceReference(report, { project_id: 'infra', area: 'cron-jobs',
    failure_mode: 'duplicate registration', summary: 'Cron job registered twice' }), false);
  assert.equal(helpers.validRecurrenceReference(report, null), false);
  assert.equal(helpers.semanticCategory({ project_id: 'infra', area: 'hermes fleet health',
    failure_mode: 'Nine gateway units unknown to systemd or inactive',
    summary: 'Hermes gateways are not running' }), 'infra:hermes-gateway:units-not-running');
});

test('closed monthly rollup uses weekly day slices and includes missing archive days', async () => {
  const env = store(['reports', 'occurrences', 'rollups']);
  const report = { sections: [{ id: 'summary', body: 'A daily summary' }] };
  for (const date of ['2026-08-30', '2026-08-31', '2026-09-01']) {
    env.data.get('dev_journal_reports').push({ report_key: `${date}:gen`, report_date: date,
      received_at: `${date}T06:00:00Z`, content_sha256: date, payload: JSON.stringify({ report }) });
  }
  env.data.get('dev_journal_occurrences').push({ occurrence_id: 'triage:1',
    fingerprint: 'infra:dev-activity:unclassified', report_date: '2026-08-31',
    project_id: 'infra', area: 'dev-activity', failure_mode: 'unclassified',
    summary: 'Triage Dev Journal finding: git scope is all-refs', status: 'triage' });
  const RealDate = Date;
  const FixedDate = class extends RealDate {
    constructor(...args) { super(...(args.length ? args : ['2026-09-08T12:00:00Z'])); }
  };
  const fn = new AsyncFunction('helpers', '$input', 'ctx', 'Date', code('rollup'));
  const out = await fn(env.helpers, { all: () => [{ json: {} }], first: () => ({ json: {} }) }, {}, FixedDate);
  assert.ok(out[0].json.weekly_updated >= 1);
  const month = env.data.get('dev_journal_rollups').find((r) => r.period_key === 'monthly:2026-08');
  assert.ok(month);
  const days = JSON.parse(month.day_slices);
  assert.equal(days.length, 31);
  assert.ok(days.every((slice) => slice.date.startsWith('2026-08')));
  assert.equal(days.find((slice) => slice.date === '2026-08-29').present, false);
  assert.match(month.content, /Triage observations: 1/);
  assert.match(month.content, /Unclassified observations:/);
  assert.doesNotMatch(month.content, /infra:dev-activity:unclassified: 1 day/);
});

test('unmatched source routes to Infra and a replay does not create a second ticket or comment', async () => {
  const env = store(['reports', 'findings', 'occurrences']);
  const date = '2026-09-23';
  const reportKey = `${date}:gen`;
  env.data.get('dev_journal_reports').push({ report_key: reportKey, report_date: date,
    source_event_id: 'evt-1', run_id: 'run-1', generation_id: 'gen', status: 'processing',
    backfill: false, payload: JSON.stringify({ markdown: '# Full journal',
      report: { report_date: date }, collector_facts: [] }), errors: '[]' });
  const issues = [], comments = [];
  env.helpers.httpRequestWithAuthentication = async (_credentialType, request) => {
    env.calls.plane ??= [];
    env.calls.plane.push(request);
    const { url, method } = request;
    if (url.endsWith('/states/')) return { results: [
      { id: 'backlog', name: 'Backlog', group: 'backlog' },
      { id: 'done', name: 'Done', group: 'completed' },
    ] };
    if (url.endsWith('/issues/') && method === 'GET') return { results: issues };
    if (url.endsWith('/issues/') && method === 'POST') {
      const created = { id: 'issue-1', sequence_id: 42, state: 'backlog',
        description_html: request.body.description_html };
      issues.push(created);
      return created;
    }
    if (url.endsWith('/comments/') && method === 'GET') return { results: comments };
    if (url.endsWith('/comments/') && method === 'POST') {
      comments.push({ comment_html: request.body.comment_html });
      return { id: `comment-${comments.length}` };
    }
    throw new Error(`Unexpected Plane ${method} ${url}`);
  };
  const llm = { choices: [{ message: { content: JSON.stringify({ findings: [{
    project_id: 'james-brennan', area: 'credential-broker', failure_mode: 'rejects-github-token',
    status: 'open', severity: 'high', summary: 'Credential broker rejects GitHub token',
    evidence: ['Broker rejected the token'], source_ids: ['needs-you:1'],
  }], non_issues: [] }) } }] };
  const ctx = { selected: { report_key: reportKey,
    source_candidates: [{ id: 'needs-you:1', text: 'Broker rejected the token' }] } };
  const first = await execute('process-report', env, [llm], ctx);
  assert.equal(first[0].json.status, 'complete');
  assert.equal(issues.length, 1);
  assert.equal(comments.length, 1);
  assert.ok(env.calls.plane.find((request) => request.method === 'POST' &&
    request.url.endsWith('/issues/')).url.includes('/projects/infra-board/'));
  assert.equal(env.data.get('dev_journal_occurrences')[0].project_id, 'james-brennan');
  await execute('process-report', env, [llm], ctx);
  assert.equal(issues.length, 1);
  assert.equal(comments.length, 1);
  assert.equal(env.data.get('dev_journal_occurrences').length, 1);
});

test('uncovered collector caveat stays in triage without Plane ticket or incident event', async () => {
  const env = store(['reports', 'findings', 'occurrences']);
  const date = '2026-09-23';
  env.data.get('dev_journal_reports').push({ report_key: `${date}:gen`, report_date: date,
    source_event_id: 'evt-1', status: 'processing', backfill: false,
    payload: JSON.stringify({ markdown: '# Journal', report: {}, collector_facts: [] }), errors: '[]' });
  env.helpers.httpRequestWithAuthentication = async () => {
    throw new Error('Plane must not be called for unclassified observations');
  };
  const llm = { choices: [{ message: { content: JSON.stringify({ findings: [], non_issues: [] }) } }] };
  const result = await execute('process-report', env, [llm], { selected: {
    report_key: `${date}:gen`, source_candidates: [{ id: 'collector:dev-activity:caveat:1',
      text: 'git scope is all-refs' }],
  } });
  assert.equal(result[0].json.status, 'complete');
  assert.match(result[0].json.final_markdown, /Triage observations: 1/);
  assert.equal(env.data.get('dev_journal_findings')[0].status, 'triage');
  assert.equal(env.data.get('dev_journal_occurrences')[0].status, 'triage');
  assert.deepEqual(await execute('incident-outbox', env, [{}]), []);
});

test('streamed extractor answer is read from the SSE text n8n hands over', async () => {
  const date = '2026-09-23';
  const run = async (llm) => {
    const env = store(['reports', 'findings', 'occurrences']);
    env.data.get('dev_journal_reports').push({ report_key: `${date}:gen`, report_date: date,
      source_event_id: 'evt-1', status: 'processing', backfill: false,
      payload: JSON.stringify({ markdown: '# Journal', report: {}, collector_facts: [] }), errors: '[]' });
    const result = await execute('process-report', env, [llm], { selected: {
      report_key: `${date}:gen`, source_candidates: [{ id: 'needs-you:1', text: 'nothing to do' }],
    } });
    return result[0].json;
  };
  const answer = JSON.stringify({ findings: [], non_issues: [{ source_id: 'needs-you:1', reason: 'informational' }] });
  const chunk = (delta) => `data: ${JSON.stringify({ object: 'chat.completion.chunk', choices: [{ index: 0, delta }] })}`;
  const sse = [': heartbeat', chunk({ role: 'assistant', reasoning_content: 'thinking' }),
    chunk({ content: answer.slice(0, 20) }), '', chunk({ content: answer.slice(20) }), 'data: [DONE]', ''].join('\n');
  const streamed = await run({ data: sse });
  assert.equal(streamed.status, 'complete');
  assert.doesNotMatch(streamed.final_markdown, /extraction pending/i);
  const errored = await run({ data: `data: ${JSON.stringify({ error: { message: 'upstream gone' } })}\n\n` });
  assert.match(errored.final_markdown, /Issue extraction pending: .*upstream gone/);
  const jsonText = await run({ data: JSON.stringify({ choices: [{ message: { content: answer } }] }) });
  assert.equal(jsonText.status, 'complete');
});

test('07:00 deadline targets yesterday and sends one missing-report alert', async () => {
  const env = store(['reports', 'occurrences']);
  const fixed = new Date('2026-09-24T11:00:00Z').getTime();
  const RealDate = Date;
  const FixedDate = class extends RealDate {
    constructor(...args) { super(...(args.length ? args : [fixed])); }
    static now() { return fixed; }
  };
  const fn = new AsyncFunction('helpers', '$input', 'ctx', 'Date', code('deadline-mail'));
  const input = { all: () => [{ json: {} }], first: () => ({ json: {} }) };
  const first = await fn(env.helpers, input, {}, FixedDate);
  assert.equal(first[0].json.report_date, '2026-09-23');
  assert.match(first[0].json.email.subject, /report missing/);
  assert.equal(env.data.get('dev_journal_reports')[0].report_key, '2026-09-23:missing');
  assert.ok(env.calls.http.some((request) => request.url.endsWith('/daily/2026-09-23')));
  await execute('record-email', env, [{ id: 'resend-id' }], { mail: first[0].json });
  assert.deepEqual(await fn(env.helpers, input, {}, FixedDate), []);
});

test('partial mail slot ignores diagnostic churn (observed executions 583338/583558)', async () => {
  const env = store(['reports']);
  const row = { report_key: '2026-10-24:c3fe75fa68ad44fba61754384e9d8320',
    report_date: '2026-10-24', content_sha256: '7029aa34259045adf31e7cda992d3768924001620ba65a45d8bb17fca005b38a',
    email_id: '01a1a3cb-c2d3-7039-857e-58a25efcfe96', email_hash: 'bff04773', errors: '[]' };
  env.data.get('dev_journal_reports').push(row);
  assert.deepEqual(await execute('prepare-email', env, [{ ...row, status: 'partial',
    final_markdown: 'Changed rate-limit errors/order', email_hash: 'c9f370b2' }]), []);
});

test('one initial partial, one completion; different generation remains deliverable', async () => {
  const env = store(['reports']);
  const row = { report_key: '2026-10-24:gen1', report_date: '2026-10-24',
    content_sha256: 'abc', errors: '[]', status: 'partial' };
  env.data.get('dev_journal_reports').push(row);
  const prepare = (status, text = status) => execute('prepare-email', env,
    [{ report_key: row.report_key, report_date: row.report_date, status, final_markdown: text }]);
  const first = (await prepare('partial'))[0].json;
  await execute('record-email', env, [{ id: 'partial-id' }], { mail: first });
  assert.deepEqual(await prepare('partial', 'different'), []);
  const completion = (await prepare('complete'))[0].json;
  assert.notEqual(first.mail_key, completion.mail_key);
  assert.match(completion.email.subject, /^Updated:/);
  await execute('record-email', env, [{ id: 'complete-id' }], { mail: completion });
  assert.deepEqual(await prepare('complete', 'model reordered same outcome'), []);
  assert.deepEqual(await prepare('partial', 'later transient failure'), []);
  const correction = { ...row, report_key: '2026-10-24:gen2', content_sha256: 'def',
    email_id: '', email_hash: '', email_phase: '', pending_email: '' };
  env.data.get('dev_journal_reports').push(correction);
  const newMail = await execute('prepare-email', env, [{ ...correction, status: 'complete', final_markdown: 'Correction' }]);
  assert.equal(newMail.length, 1);
  assert.notEqual(newMail[0].json.mail_key, completion.mail_key);
});

test('provider failure/restart replays frozen payload and key, not changing markdown', async () => {
  let env = store(['reports']);
  const row = { report_key: '2026-10-24:gen1', report_date: '2026-10-24', errors: '[]' };
  env.data.get('dev_journal_reports').push(row);
  const processed = { ...row, status: 'partial', final_markdown: 'original' };
  const first = (await execute('prepare-email', env, [processed]))[0].json;
  assert.ok(row.pending_email);
  await execute('record-email', env, [{ error: 'timeout' }], { mail: first });
  assert.equal(row.email_id, undefined);
  const saved = structuredClone(env.data.get('dev_journal_reports'));
  env = store(['reports']); env.data.set('dev_journal_reports', saved);
  const retry = (await execute('prepare-email', env, [{ ...processed, status: 'complete', final_markdown: 'changed' }]))[0].json;
  assert.deepEqual(retry, first);
  await execute('record-email', env, [{ id: 'accepted' }], { mail: retry });
  assert.equal(env.data.get('dev_journal_reports')[0].pending_email, '');
});

test('legacy completion and backfill remain quiet; completed partial receipt gets selected', async () => {
  const env = store(['reports', 'findings']);
  const row = { report_key: '2026-10-24:gen1', report_date: '2026-10-24',
    generation_id: 'gen1', email_id: 'old', email_phase: 'complete', status: 'complete',
    payload: JSON.stringify({ markdown: '# Journal', report: {}, collector_facts: [] }) };
  env.data.get('dev_journal_reports').push(row);
  assert.deepEqual(await execute('select-report', env, [{}]), []);
  assert.deepEqual(await execute('prepare-email', env, [{ ...row, final_markdown: 'same' }]), []);
  row.email_phase = 'partial';
  assert.equal((await execute('select-report', env, [{}])).length, 1);
  assert.deepEqual(await execute('prepare-email', env, [{ ...row, backfill: true }]), []);
});

test('all four journal clocks are inactive POST webhooks paired with disabled Candybar manifests', () => {
  for (const lane of ['processor', 'deadline', 'incidents', 'rollups']) {
    const wf = JSON.parse(read(`${lane}.workflow.json`));
    assert.equal(wf.active, false);
    assert.ok(!wf.nodes.some(n => /scheduleTrigger|\.cron$/.test(n.type)));
    const webhook = wf.nodes.find(n => n.type === 'n8n-nodes-base.webhook');
    assert.equal(webhook.parameters.httpMethod, 'POST');
    assert.equal(webhook.parameters.path, `candybar/dev-journal-${lane}`);
    assert.equal(webhook.parameters.responseMode, 'lastNode');
    assert.match(read(`candybar/${lane}/candybar.toml`), /enabled = false/);
    assert.match(read(`candybar/${lane}/candybar.toml`), /kind = "n8n"/);
  }
  const processor = JSON.parse(read('processor.workflow.json'));
  assert.equal(processor.nodes.find(n => n.name === 'Claim one report and prepare extraction').parameters.operation, 'selectReport');
});

test('deadline and processor share a frozen partial slot, then allow completion', async () => {
  const env = store(['reports', 'occurrences']);
  const row = { report_key: '2026-09-23:gen', report_date: '2026-09-23', generation_id: 'gen',
    content_sha256: 'a', status: 'partial', errors: '["old error"]',
    payload: JSON.stringify({ markdown: '# Journal', report: {} }) };
  env.data.get('dev_journal_reports').push(row);
  const RealDate = Date;
  const FixedDate = class extends RealDate {
    constructor(...args) { super(...(args.length ? args : ['2026-09-24T11:00:00Z'])); }
    static now() { return new RealDate('2026-09-24T11:00:00Z').getTime(); }
  };
  const fn = new AsyncFunction('helpers', '$input', 'ctx', 'Date', code('deadline-mail'));
  const deadline = await fn(env.helpers, { first: () => ({ json: {} }) }, {}, FixedDate);
  const processed = { ...row, status: 'partial', final_markdown: '# Changed diagnostics' };
  const processor = await execute('prepare-email', env, [processed]);
  assert.deepEqual(processor, deadline);
  await execute('record-email', env, [{ id: 'deadline-id' }], { mail: deadline[0].json });
  assert.deepEqual(await execute('prepare-email', env, [processed]), []);
  assert.equal((await execute('prepare-email', env, [{ ...processed, status: 'complete' }])).length, 1);
});

test('direct complete delivery needs no partial and retries neither complete nor partial', async () => {
  const env = store(['reports']);
  const row = { report_key: '2026-10-24:gen', report_date: '2026-10-24', errors: '[]' };
  env.data.get('dev_journal_reports').push(row);
  const result = { ...row, status: 'complete', final_markdown: 'Done' };
  const first = (await execute('prepare-email', env, [result]))[0].json;
  assert.doesNotMatch(first.email.subject, /pending|Updated:/);
  await execute('record-email', env, [{ id: 'done-id' }], { mail: first });
  assert.deepEqual(await execute('prepare-email', env, [result]), []);
  assert.deepEqual(await execute('prepare-email', env, [{ ...result, status: 'partial' }]), []);
});
