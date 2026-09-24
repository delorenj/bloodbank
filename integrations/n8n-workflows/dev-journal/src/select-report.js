const now = Date.now();
const candidates = (await rows('reports')).filter((r) => {
  const retry = r.next_attempt_at && Date.parse(r.next_attempt_at) > now;
  const lease = r.processing_lease_until && Date.parse(r.processing_lease_until) > now;
  const done = r.status === 'complete' && (r.backfill || r.email_id);
  return r.generation_id !== 'missing' && !retry && !lease && !done;
});
candidates.sort((a, b) => {
  if (a.backfill !== b.backfill) return a.backfill ? 1 : -1;
  return a.backfill ? a.report_date.localeCompare(b.report_date) : b.report_date.localeCompare(a.report_date);
});
const row = candidates[0];
if (!row) return [];
const content = JSON.parse(row.payload);
const sourceCandidates = [];
const summary = (content.report?.sections || []).find((s) => s.id === 'summary')?.body || '';
const needs = summary.match(/## Needs you\s*\n([\s\S]*?)(?=\n## |$)/i)?.[1] || '';
for (const [index, line] of needs.split('\n').filter((line) => /^\s*[-*]\s+/.test(line)).entries()) {
  sourceCandidates.push({ id: `needs-you:${index + 1}`, text: line.replace(/^\s*[-*]\s+/, '').trim() });
}
for (const fact of content.collector_facts) {
  if (fact.status !== 'complete' && fact.status !== 'ok') sourceCandidates.push({
    id: `collector:${fact.id}:status`, text: `${fact.status}: ${fact.summary || fact.reason || ''}`,
  });
  for (const [index, caveat] of (fact.caveats || []).entries()) sourceCandidates.push({
    id: `collector:${fact.id}:caveat:${index + 1}`, text: String(caveat),
  });
}
await upsert('reports', 'report_key', row.report_key, {
  report_key: row.report_key, status: 'processing', attempts: Number(row.attempts || 0) + 1,
  processing_lease_until: new Date(now + 4 * 60 * 1000).toISOString(),
});
const system = `You extract operational issues from a nightly Dev Journal. Return ONLY a JSON object with arrays findings and non_issues. Read the full journal and structured collector facts. Every source candidate ID must occur exactly once, either in a finding.source_ids or a non_issues.source_id. Group duplicate descriptions of one problem into one finding. Only unresolved/actionable problems get status=open. Explicitly verified fixes get status=resolved. Historical commit messages, completed ticket descriptions and old incidents described as fixed are context, never new open problems. For each finding provide project_id (repo slug if known, otherwise infra), area, precise failure_mode, summary, severity (low|medium|high|critical), status (open|resolved), evidence (1-20 short literal excerpts), source_ids, optional existing_ticket_key, and resolution_evidence for resolved. Non_issues entries require source_id and reason. Do not invent evidence. A collector caveat about data scope may be informational; a broken service, missing delivery, failed tick, or uncorroborated claim requiring investigation is actionable.`;
const user = JSON.stringify({ report_date: row.report_date, source_candidates: sourceCandidates,
  collector_facts: content.collector_facts, full_report_markdown: content.markdown });
const llmRequest = {
  model: '@preset/opencode-budget-under-3m-output',
  messages: [{ role: 'system', content: system }, { role: 'user', content: user }],
  response_format: { type: 'json_object' }, temperature: 0, max_tokens: 10000,
};
return [{ json: { report_key: row.report_key, source_candidates: sourceCandidates,
  llm_request: llmRequest } }];
