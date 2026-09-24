import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const common = readFileSync(join(here, 'src', 'common.js'), 'utf8');
const source = (name) => `${common}\n${readFileSync(join(here, 'src', `${name}.js`), 'utf8')}`;
const uuid = (name) => {
  const h = createHash('sha256').update(`dev-journal:${name}`).digest('hex');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-4${h.slice(13, 16)}-a${h.slice(17, 20)}-${h.slice(20, 32)}`;
};
const node = (name, type, parameters, x, y, extra = {}) => ({
  id: uuid(name), name, type, typeVersion: type === 'n8n-nodes-base.code' ? 2 :
    type === 'n8n-nodes-base.scheduleTrigger' ? 1.2 :
    type === 'n8n-nodes-base.httpRequest' ? 4.2 : 1,
  position: [x, y], parameters, ...extra,
});
const code = (name, file, x, y, extra = {}) => node(name, 'n8n-nodes-base.code', {
  mode: 'runOnceForAllItems', jsCode: source(file),
}, x, y, extra);
const journal = (name, operation, x, y, parameters = {}, extra = {}) =>
  node(name, 'n8n-nodes-bloodbank.devJournal', { operation, ...parameters }, x, y, extra);
const schedule = (name, expression, x = 0, y = 0) => node(name, 'n8n-nodes-base.scheduleTrigger', {
  rule: { interval: [{ field: 'cronExpression', expression }] },
}, x, y);
const manual = (name, x = 0, y = 0) => node(name, 'n8n-nodes-base.manualTrigger', {}, x, y);
const bloodbank = (name, event, data, x, y) => node(name, 'n8n-nodes-bloodbank.bloodbank', {
  mode: 'event', event, data, service: 'n8n-dev-journal', connection: {},
}, x, y);
const trigger = (name, x, y) => node(name, 'n8n-nodes-bloodbank.bloodbankTrigger', {
  events: ['bloodbank.reporting.report.completed'],
  dataMatch: { conditions: [{ path: 'producer', values: 'delonet-daily-report' }] },
  delivery: 'durable', acknowledge: 'afterExecution', catchUpHours: 0, connection: {},
}, x, y);
const planeCredential = { httpHeaderAuth: { id: 'TpxVVgOnmjHaE6h7', name: 'Plane API (33GOD + AutomaticAI)' } };
const newApiCredential = { httpHeaderAuth: { id: 'DjNewApiN8n2026', name: 'Dev Journal NewAPI n8n' } };
const resendCredential = { httpHeaderAuth: { id: 'ZYgVKEU9w9je0Jt4', name: 'Resend HTTP' } };
const link = (connections, from, to) => {
  const entry = connections[from] ?? { main: [[]] };
  entry.main[0].push({ node: to, type: 'main', index: 0 });
  connections[from] = entry;
};
const workflow = (name, nodes, edges, description) => {
  const connections = {};
  for (const [from, to] of edges) link(connections, from, to);
  return { id: uuid(`workflow:${name}`), name, nodes, connections,
    settings: { executionOrder: 'v1', timezone: 'America/New_York' },
    active: false, pinData: {}, meta: { templateCredsSetupCompleted: true },
    description, tags: [] };
};
const ingressNodes = [
  trigger('Report completed', 0, 0),
  journal('Persist verified snapshot', 'ingest', 240, 0),
  bloodbank('Journal received receipt', 'bloodbank.reporting.journal.received',
    '={{ $json.receipt }}', 480, 0),
];
const backfillDatesCommand = `python3 - <<'PY'
import json, pathlib, subprocess
config = pathlib.Path('/home/delorenj/.config/delonet-daily-report/report.json')
cfg = json.loads(config.read_text())
reportctl = '/home/delorenj/.hermes/skills/delonet-daily-report/scripts/reportctl'
for pointer in sorted(pathlib.Path(cfg['archive_dir']).glob('[0-9][0-9][0-9][0-9]/*/*/current.json')):
    date = pointer.parent.name
    result = subprocess.run([reportctl, '--config', str(config), 'verify', '--date', date], text=True, capture_output=True)
    if result.returncode == 0 and json.loads(result.stdout).get('ok'):
        print(date)
PY`;
const backfillNodes = [
  manual('Start verified archive backfill'),
  node('List verified report dates', 'n8n-nodes-base.executeCommand',
    { command: backfillDatesCommand }, 240, 0),
  code('Validate archive dates', 'backfill-dates', 480, 0),
  node('Export one verified snapshot per item', 'n8n-nodes-base.executeCommand', {
    executeOnce: false,
    command: "={{ '/home/delorenj/.hermes/skills/delonet-daily-report/scripts/reportctl --config /home/delorenj/.config/delonet-daily-report/report.json export-snapshot --date ' + $json.date }}",
  }, 720, 0),
  code('Parse snapshots', 'backfill-parse', 960, 0),
  journal('Persist verified snapshots', 'ingest', 1200, 0),
  bloodbank('Backfill received receipts', 'bloodbank.reporting.journal.received',
    '={{ $json.receipt }}', 1440, 0),
];

const extractor = node('Extract and classify findings', 'n8n-nodes-base.httpRequest', {
  method: 'POST', url: 'https://api.automaticai.io/v1/chat/completions',
  authentication: 'genericCredentialType', genericAuthType: 'httpHeaderAuth',
  sendBody: true, contentType: 'json', specifyBody: 'json',
  jsonBody: '={{ JSON.stringify($json.llm_request) }}',
  options: { timeout: 180000 },
}, 480, 0, { credentials: newApiCredential, onError: 'continueRegularOutput' });
const resend = (name, x, y) => node(name, 'n8n-nodes-base.httpRequest', {
  method: 'POST', url: 'https://api.resend.com/emails',
  authentication: 'genericCredentialType', genericAuthType: 'httpHeaderAuth',
  sendHeaders: true,
  headerParameters: { parameters: [{ name: 'Idempotency-Key', value: '={{ $json.mail_key }}' }] },
  sendBody: true, contentType: 'json', specifyBody: 'json',
  jsonBody: '={{ JSON.stringify($json.email) }}',
  options: { timeout: 30000 },
}, x, y, { credentials: resendCredential, onError: 'continueRegularOutput' });
const processorNodes = [
  schedule('Find reports to process', '*/5 * * * *'),
  manual('Process next report manually', 0, 120),
  journal('Claim one report and prepare extraction', 'selectReport', 240, 0),
  extractor,
  journal('Track issues and publish Infra note', 'processReport', 720, 0, {
    selected: "={{ $('Claim one report and prepare extraction').first().json }}",
  }, { credentials: planeCredential }),
  journal('Only live mail not yet sent', 'prepareEmail', 960, 0),
  resend('Send daily journal', 1200, 0),
  journal('Store Resend receipt', 'recordEmail', 1440, 0, {
    mail: "={{ $('Only live mail not yet sent').first().json }}",
  }),
];
const deadlineNodes = [
  schedule('Daily 07:00 deadline', '0 7 * * *'),
  journal('Prepare honest partial if pending', 'deadlineMail', 240, 0),
  resend('Send pending daily journal', 480, 0),
  journal('Store deadline receipt', 'recordEmail', 720, 0, {
    mail: "={{ $('Prepare honest partial if pending').first().json }}",
  }),
];
const incidentNodes = [
  schedule('Retry incident outbox', '*/5 * * * *', 0, -100),
  manual('Replay incident outbox manually', 0, 100),
  journal('Load unsent incidents', 'incidentOutbox', 240, 0),
  bloodbank('Publish observed incident', 'bloodbank.reporting.incident.observed',
    '={{ $json.event }}', 480, 0),
  journal('Record incident publication', 'recordIncident', 720, 0, {
    incidents: "={{ $('Load unsent incidents').all().map(item => item.json) }}",
  }),
];
const rollupNodes = [
  schedule('Refresh closed rollups', '30 8 * * *', 0, -100),
  manual('Backfill rollups', 0, 100),
  journal('Upsert weekly and monthly Infra notes', 'rollup', 240, 0),
];
const backfillFinalizerNodes = [
  manual('Reconcile unresolved backfill findings'),
  journal('Create tickets only for unresolved history', 'backfillFinalize', 240, 0,
    {}, { credentials: planeCredential }),
];

const bundles = {
  'ingress.workflow.json': workflow('Dev Journal — Bloodbank Ingress', ingressNodes,
    [['Report completed', 'Persist verified snapshot'], ['Persist verified snapshot', 'Journal received receipt']],
    'Durable receipt after the portable report snapshot is stored.'),
  'backfill.workflow.json': workflow('Dev Journal — Verified Archive Backfill', backfillNodes,
    [['Start verified archive backfill', 'List verified report dates'],
      ['List verified report dates', 'Validate archive dates'],
      ['Validate archive dates', 'Export one verified snapshot per item'],
      ['Export one verified snapshot per item', 'Parse snapshots'],
      ['Parse snapshots', 'Persist verified snapshots'], ['Persist verified snapshots', 'Backfill received receipts']],
    'Run manually once; imports verified current generations without historical email or a second report.completed event.'),
  'processor.workflow.json': workflow('Dev Journal — Process Reports', processorNodes,
    [['Find reports to process', 'Claim one report and prepare extraction'],
      ['Process next report manually', 'Claim one report and prepare extraction'],
      ['Claim one report and prepare extraction', 'Extract and classify findings'],
      ['Extract and classify findings', 'Track issues and publish Infra note'],
      ['Track issues and publish Infra note', 'Only live mail not yet sent'],
      ['Only live mail not yet sent', 'Send daily journal'],
      ['Send daily journal', 'Store Resend receipt']],
    'Extracts every actionable issue, maintains one active Plane ticket per fingerprint, upserts Infra notes, and sends live mail.'),
  'deadline.workflow.json': workflow('Dev Journal — 07:00 Partial Mail', deadlineNodes,
    [['Daily 07:00 deadline', 'Prepare honest partial if pending'],
      ['Prepare honest partial if pending', 'Send pending daily journal'],
      ['Send pending daily journal', 'Store deadline receipt']],
    'Sends an explicit pending report if the scheduled issue processor has not completed by 07:00 New York time.'),
  'incidents.workflow.json': workflow('Dev Journal — Incident Outbox', incidentNodes,
    [['Retry incident outbox', 'Load unsent incidents'],
      ['Replay incident outbox manually', 'Load unsent incidents'],
      ['Load unsent incidents', 'Publish observed incident'],
      ['Publish observed incident', 'Record incident publication']],
    'Retries schema-validated incident facts independently of Plane, notebook, and mail.'),
  'rollups.workflow.json': workflow('Dev Journal — Calendar Rollups', rollupNodes,
    [['Refresh closed rollups', 'Upsert weekly and monthly Infra notes'],
      ['Backfill rollups', 'Upsert weekly and monthly Infra notes']],
    'Calendar weeks and months in America/New_York; monthly notes use per-day slices from completed weekly rollups.'),
  'backfill-finalize.workflow.json': workflow('Dev Journal — Backfill Unresolved Tickets',
    backfillFinalizerNodes,
    [['Reconcile unresolved backfill findings', 'Create tickets only for unresolved history']],
    'Run after every historical report has processed; creates Plane tickets only for fingerprints whose latest status remains open.'),
};
for (const [file, body] of Object.entries(bundles)) {
  writeFileSync(join(here, file), `${JSON.stringify(body, null, 2)}\n`);
}
const programPath = join(here, '..', '..', 'n8n-nodes-bloodbank', 'src', 'nodes', 'DevJournal');
mkdirSync(programPath, { recursive: true });
const programs = Object.fromEntries([
  ['ingest', 'ingest'], ['selectReport', 'select-report'], ['processReport', 'process-report'],
  ['prepareEmail', 'prepare-email'],
  ['deadlineMail', 'deadline-mail'], ['recordEmail', 'record-email'],
  ['incidentOutbox', 'incident-outbox'], ['recordIncident', 'record-incident'],
  ['rollup', 'rollup'], ['backfillFinalize', 'backfill-finalize'],
].map(([operation, file]) => [operation, source(file)]));
writeFileSync(join(programPath, 'programs.generated.ts'),
  `// Generated by integrations/n8n-workflows/dev-journal/build.mjs.\n` +
  `export const journalPrograms: Record<string, string> = ${JSON.stringify(programs, null, 2)};\n`);
console.log(`Generated ${Object.keys(bundles).length} n8n workflows`);
