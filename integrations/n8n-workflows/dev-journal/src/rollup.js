function day(value) { return new Date(`${value}T12:00:00Z`); }
function date(value) { return value.toISOString().slice(0, 10); }
function plus(value, count) { const d = day(value); d.setUTCDate(d.getUTCDate() + count); return date(d); }
function weekStart(value) { const d = day(value); return plus(value, -((d.getUTCDay() + 6) % 7)); }
function monthEnd(value) { const d = day(`${value.slice(0, 7)}-01`); d.setUTCMonth(d.getUTCMonth() + 1); d.setUTCDate(0); return date(d); }
function monthNext(value) { const d = day(`${value.slice(0, 7)}-01`); d.setUTCMonth(d.getUTCMonth() + 1); return date(d); }
function summaries(slices) {
  const areaCounts = new Map(), problemDays = new Map();
  for (const slice of slices) for (const problem of slice.incidents || []) {
    if (problem.status === 'triage') continue;
    const area = canonicalArea(problem.area);
    const id = problem.semantic_id || semanticCategory(problem) || problem.fingerprint;
    areaCounts.set(area, (areaCounts.get(area) || 0) + 1);
    if (!problemDays.has(id)) problemDays.set(id, new Set());
    problemDays.get(id).add(slice.date);
  }
  const areas = [...areaCounts].sort((a, b) => b[1] - a[1]);
  const recurring = [...problemDays].filter(([, days]) => days.size > 1)
    .sort((a, b) => b[1].size - a[1].size);
  return [
    `Reports: ${slices.filter((s) => s.present).length}/${slices.length}. ` +
      `Incident occurrences: ${slices.reduce((n, s) => n + s.incidents.filter((i) => i.status !== 'triage').length, 0)}. ` +
      `Triage observations: ${slices.reduce((n, s) => n + s.incidents.filter((i) => i.status === 'triage').length, 0)}.`,
    '## Problem areas',
    ...(areas.length ? areas.map(([area, count]) => `- ${area}: ${count} occurrence(s)`) : ['- No recorded incidents.']),
    '## Recurring problems',
    ...(recurring.length ? recurring.map(([id, days]) => `- ${id}: ${days.size} day(s)`) : ['- No problem appeared on multiple days.']),
  ];
}
function render(kind, start, end, slices) {
  const title = kind === 'weekly' ? `Dev Journal Weekly — ${start} to ${end}` :
    `Dev Journal Monthly — ${start.slice(0, 7)}`;
  const lines = [`# ${title}`, `Period: ${start} through ${end} (America/New_York).`,
    ...summaries(slices), '## Daily record'];
  for (const slice of slices) {
    lines.push(`### ${slice.date}`);
    if (!slice.present) { lines.push('No verified published daily report.'); continue; }
    lines.push(slice.summary || 'No summary section.');
    const issues = slice.incidents.filter((issue) => issue.status !== 'triage');
    const triage = slice.incidents.filter((issue) => issue.status === 'triage');
    if (issues.length) lines.push('Issues:', ...issues.map((issue) =>
      `- ${issue.status.toUpperCase()} ${issue.summary}${issue.ticket_key ? ` (${issue.ticket_key})` : ''}`));
    if (triage.length) lines.push('Unclassified observations:', ...triage.map((issue) =>
      `- ${issue.summary}`));
  }
  return { title, content: `${lines.join('\n\n')}\n` };
}
async function write(kind, key, start, end, slices, existing) {
  const sourceSignature = digest(JSON.stringify(slices));
  const periodKey = `${kind}:${key}`;
  const previous = existing.get(periodKey);
  if (previous?.source_signature === sourceSignature && previous.note_id) return false;
  const rendered = render(kind, start, end, slices);
  const result = unwrap(await helpers.httpRequest({ method: 'PUT', json: true,
    url: `http://127.0.0.1:8775/v1/projects/infra/notes/${kind}/${key}`,
    headers: { 'Content-Type': 'application/json' },
    body: rendered, timeout: 30000 }));
  if (!result.note_id) throw new Error(`Infra notebook did not return note_id for ${periodKey}`);
  const row = await upsert('rollups', 'period_key', periodKey, {
    period_key: periodKey, kind, start_date: start, end_date: end,
    source_signature: sourceSignature, content: rendered.content,
    day_slices: JSON.stringify(slices), note_id: result.note_id, updated_at: isoNow(),
  });
  existing.set(periodKey, row);
  return true;
}
const reportRows = await rows('reports');
if (!reportRows.length) return [];
const byDay = new Map();
for (const row of reportRows) {
  const previous = byDay.get(row.report_date);
  if (!previous || String(row.received_at) > String(previous.received_at)) byDay.set(row.report_date, row);
}
const occurrences = await rows('occurrences');
const incidentsByDay = new Map();
for (const row of occurrences) {
  const list = incidentsByDay.get(row.report_date) || [];
  list.push({ fingerprint: row.fingerprint, area: row.area, status: row.status,
    project_id: row.project_id, failure_mode: row.failure_mode,
    semantic_id: semanticCategory(row) || row.fingerprint,
    severity: row.severity, summary: row.summary, ticket_key: row.ticket_key || '' });
  incidentsByDay.set(row.report_date, list);
}
const existing = new Map((await rows('rollups')).map((row) => [row.period_key, row]));
const today = nyDate();
const closedSunday = plus(weekStart(today), -1);
const firstDate = [...byDay.keys()].sort()[0];
let weeklyUpdated = 0, monthlyUpdated = 0;
for (let start = weekStart(`${firstDate.slice(0, 7)}-01`); plus(start, 6) <= closedSunday; start = plus(start, 7)) {
  const end = plus(start, 6);
  const slices = [];
  for (let current = start; current <= end; current = plus(current, 1)) {
    const row = byDay.get(current);
    const content = row ? JSON.parse(row.payload) : null;
    const summary = content?.report?.sections?.find((s) => s.id === 'summary')?.body || '';
    slices.push({ date: current, present: Boolean(row),
      content_sha256: row?.content_sha256 || '', summary: summary.slice(0, 5000),
      incidents: incidentsByDay.get(current) || [] });
  }
  if (await write('weekly', start, start, end, slices, existing)) weeklyUpdated++;
}
for (let start = `${firstDate.slice(0, 7)}-01`; start < today; start = monthNext(start)) {
  const end = monthEnd(start);
  if (plus(weekStart(end), 6) > closedSunday) continue;
  const slices = [];
  for (const row of existing.values()) {
    if (row.kind !== 'weekly') continue;
    for (const slice of JSON.parse(row.day_slices || '[]')) {
      if (slice.date >= start && slice.date <= end) slices.push(slice);
    }
  }
  slices.sort((a, b) => a.date.localeCompare(b.date));
  const expected = Number(end.slice(-2));
  if (slices.length !== expected) throw new Error(`Month ${start.slice(0, 7)} lacks completed weekly day slices`);
  if (await write('monthly', start.slice(0, 7), start, end, slices, existing)) monthlyUpdated++;
}
return [{ json: { weekly_updated: weeklyUpdated, monthly_updated: monthlyUpdated,
  closed_through: closedSunday } }];
