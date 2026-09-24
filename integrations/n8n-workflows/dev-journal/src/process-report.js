const selected = ctx.selected;
const reportRow = await one('reports', 'report_key', selected.report_key);
if (!reportRow) throw new Error(`Report row disappeared: ${selected.report_key}`);
const content = JSON.parse(reportRow.payload);
const date = reportRow.report_date;
const sourceCandidates = selected.source_candidates || [];
const errors = [];
const handled = [];
let extracted = { findings: [], non_issues: [] };
const response = $input.first().json || {};
try {
  if (response.error) throw new Error(JSON.stringify(response.error));
  let message = response.choices?.[0]?.message?.content;
  if (Array.isArray(message)) message = message.map((v) => v.text || '').join('');
  if (typeof message !== 'string') throw new Error('OpenRouter returned no assistant message');
  message = message.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '');
  extracted = JSON.parse(message);
  if (!Array.isArray(extracted.findings) || !Array.isArray(extracted.non_issues)) {
    throw new Error('Extractor response is missing findings/non_issues arrays');
  }
} catch (error) {
  errors.push(`Issue extraction pending: ${String(error.message || error).slice(0, 240)}`);
  extracted = { findings: [], non_issues: [] };
}

const allSourceIds = new Set(sourceCandidates.map((candidate) => candidate.id));
const covered = new Set();
const normalized = [];
for (const raw of extracted.findings) {
  if (!raw || !['open', 'resolved'].includes(raw.status)) continue;
  const ids = (Array.isArray(raw.source_ids) ? raw.source_ids : []).filter((id) => allSourceIds.has(id));
  for (const id of ids) covered.add(id);
  const evidence = (Array.isArray(raw.evidence) ? raw.evidence : [])
    .map((value) => String(value).trim()).filter(Boolean).slice(0, 20);
  if (!evidence.length) {
    evidence.push(...ids.map((id) => sourceCandidates.find((candidate) => candidate.id === id)?.text).filter(Boolean).slice(0, 20));
  }
  if (!evidence.length) continue;
  normalized.push({
    project_id: String(raw.project_id || 'infra').toLowerCase().replace(/[^a-z0-9-]+/g, '-').slice(0, 80),
    area: String(raw.area || 'unknown').trim().slice(0, 120),
    failure_mode: String(raw.failure_mode || raw.summary || 'requires triage').trim().slice(0, 180),
    status: raw.status,
    severity: ['low', 'medium', 'high', 'critical'].includes(raw.severity) ? raw.severity : 'medium',
    summary: String(raw.summary || raw.failure_mode || 'Dev Journal issue').trim().slice(0, 240),
    evidence,
    source_ids: ids,
    existing_ticket_key: String(raw.existing_ticket_key || '').trim().slice(0, 40),
    resolution_evidence: String(raw.resolution_evidence || '').trim().slice(0, 400),
  });
}
for (const raw of extracted.non_issues) {
  if (allSourceIds.has(raw?.source_id) && String(raw.reason || '').trim()) covered.add(raw.source_id);
}
// A valid model response may still overlook a source. An omitted candidate is
// explicitly triaged instead of disappearing from the final journal.
if (!errors.length) for (const candidate of sourceCandidates) {
  if (covered.has(candidate.id)) continue;
  normalized.push({ project_id: 'infra', area: candidate.id.split(':')[1] || 'journal',
    failure_mode: `unclassified-${candidate.id}`, status: 'open', severity: 'medium',
    summary: `Triage Dev Journal finding: ${candidate.text.slice(0, 130)}`,
    evidence: [candidate.text.slice(0, 1000)], source_ids: [candidate.id],
    existing_ticket_key: '', resolution_evidence: '' });
}

function slug(value) { return String(value).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); }
function fingerprint(f) {
  const key = [f.project_id, f.area, f.failure_mode].map(slug).join(':');
  return key.length <= 220 ? key : `${key.slice(0, 195)}:${digest(key)}`;
}
async function plane(method, url, body) {
  const opts = { method, url, json: true, headers: { 'User-Agent': 'Mozilla/5.0' } };
  if (body !== undefined) { opts.body = body; opts.headers['Content-Type'] = 'application/json'; }
  return unwrap(await helpers.httpRequestWithAuthentication('httpHeaderAuth', opts));
}
async function pages(url) {
  const out = [], seen = new Set();
  for (let i = 0; i < 100 && url; i++) {
    if (!url.startsWith('https://plane.delo.sh/api/v1/')) throw new Error('Unsafe Plane pagination URL');
    if (seen.has(url)) throw new Error('Repeated Plane pagination URL');
    seen.add(url);
    const response = await plane('GET', url);
    out.push(...(Array.isArray(response) ? response : response.results || []));
    if (response.next) url = new URL(response.next, url).href;
    else if (response.next_page_results && response.next_cursor) {
      const next = new URL(url); next.searchParams.set('cursor', response.next_cursor); url = next.href;
    } else url = null;
  }
  if (url) throw new Error('Plane pagination limit exceeded');
  return out;
}
const registry = unwrap(await helpers.httpRequest({ method: 'GET',
  url: 'http://127.0.0.1:8764/v1/registry', json: true }));
const projects = registry.projects || {};
const infra = projects.infra?.ticket_provider;
if (infra?.state !== 'linked' || infra.workspace !== '33god' || !infra.board_id) {
  throw new Error('Infra fallback board is not linked in 33god');
}
function route(projectId) {
  const p = projects[slug(projectId)]?.ticket_provider;
  return p?.type === 'plane' && p.state === 'linked' && p.workspace === '33god' && p.board_id
    ? { board_id: p.board_id, key: p.identifier || '', project_id: slug(projectId) }
    : { board_id: infra.board_id, key: infra.identifier || 'INFR', project_id: 'infra' };
}
const stateCache = new Map();
async function states(board) {
  if (!stateCache.has(board)) stateCache.set(board,
    await pages(`https://plane.delo.sh/api/v1/workspaces/33god/projects/${board}/states/`));
  return stateCache.get(board);
}
function keyOf(issue, boardKey) {
  return issue?.identifier || (issue?.sequence_id ? `${boardKey}-${issue.sequence_id}` : '');
}
async function matchTicket(board, marker, referencedKey) {
  const all = await pages(`https://plane.delo.sh/api/v1/workspaces/33god/projects/${board.board_id}/issues/`);
  const boardStates = await states(board.board_id);
  const done = new Set(boardStates.filter((s) => ['completed', 'cancelled'].includes(s.group)).map((s) => s.id));
  const active = all.filter((issue) => !done.has(issue.state));
  const byMarker = active.filter((issue) => String(issue.description_html || '').includes(marker));
  if (byMarker.length > 1) throw new Error(`Multiple active Plane issues have marker ${marker}`);
  if (byMarker.length) return byMarker[0];
  if (referencedKey.startsWith(`${board.key}-`)) {
    const sequence = Number(referencedKey.slice(board.key.length + 1));
    return active.find((issue) => issue.sequence_id === sequence) || null;
  }
  return null;
}
async function commentOnce(board, issue, occurrenceId, html) {
  const url = `https://plane.delo.sh/api/v1/workspaces/33god/projects/${board.board_id}/issues/${issue.id}/comments/`;
  const marker = `[dev-journal-occurrence:${occurrenceId}]`;
  const comments = await pages(url);
  if (!comments.some((c) => String(c.comment_html || '').includes(marker))) {
    await plane('POST', url, { comment_html: `${html}<p>${escapeHtml(marker)}</p>` });
  }
}
const known = await rows('findings');
const knownByFingerprint = new Map(known.map((row) => [row.fingerprint, row]));
const groups = new Map();
for (const finding of normalized) {
  const id = fingerprint(finding);
  const previous = groups.get(id);
  if (previous) {
    previous.evidence = [...new Set([...previous.evidence, ...finding.evidence])].slice(0, 20);
    previous.source_ids.push(...finding.source_ids);
    if (finding.status === 'open') previous.status = 'open';
  } else groups.set(id, { ...finding, fingerprint: id });
}

for (const finding of groups.values()) {
  const previous = knownByFingerprint.get(finding.fingerprint) || null;
  const occurrenceId = `${date}:${finding.fingerprint}`;
  const board = previous?.board_id ? { board_id: previous.board_id,
    key: previous.ticket_key?.split('-')[0] || route(finding.project_id).key } : route(finding.project_id);
  let ticketId = previous?.ticket_id || '';
  let ticketKey = previous?.ticket_key || '';
  let active = previous?.active === true;
  let issueError = '';
  const marker = `[dev-journal:${finding.fingerprint}]`;
  try {
    if (!reportRow.backfill) {
      const issue = await matchTicket(board, marker, finding.existing_ticket_key);
      const ticketUrl = `https://plane.delo.sh/api/v1/workspaces/33god/projects/${board.board_id}/issues/`;
      if (finding.status === 'open') {
        let target = issue;
        if (!target) {
          const boardStates = await states(board.board_id);
          const backlog = boardStates.find((s) => s.name === 'Backlog') ||
            boardStates.find((s) => s.group === 'backlog');
          if (!backlog) throw new Error(`No Backlog state on board ${board.board_id}`);
          const linked = previous?.ticket_key ? `<p>Earlier episode: ${escapeHtml(previous.ticket_key)}</p>` : '';
          const description = `<p>${escapeHtml(finding.summary)}</p><p>First observed ${date} in the Dev Journal.</p>` +
            `<ul>${finding.evidence.map((e) => `<li>${escapeHtml(e)}</li>`).join('')}</ul>` +
            `<p>${escapeHtml(marker)}</p>${linked}`;
          target = await plane('POST', ticketUrl, { name: finding.summary, state: backlog.id,
            priority: finding.severity === 'critical' ? 'urgent' : finding.severity,
            description_html: description });
        }
        ticketId = target.id;
        ticketKey = keyOf(target, board.key) || ticketKey;
        await commentOnce(board, target, occurrenceId,
          `<p>Observed again ${date} in the Dev Journal.</p><ul>${finding.evidence.map((e) =>
            `<li>${escapeHtml(e)}</li>`).join('')}</ul>`);
        active = true;
      } else if (issue) {
        ticketId = issue.id;
        ticketKey = keyOf(issue, board.key) || ticketKey;
        if (!finding.resolution_evidence) throw new Error('Resolution lacks explicit verification evidence');
        await commentOnce(board, issue, occurrenceId,
          `<p>Resolution verified in the ${date} Dev Journal: ${escapeHtml(finding.resolution_evidence)}</p>`);
        const boardStates = await states(board.board_id);
        const done = boardStates.find((s) => s.name === 'Done') ||
          boardStates.find((s) => s.group === 'completed');
        if (!done) throw new Error(`No Done state on board ${board.board_id}`);
        if (issue.state !== done.id) await plane('PATCH', `${ticketUrl}${issue.id}/`, { state: done.id });
        active = false;
      } else active = false;
    } else if (finding.status === 'resolved') active = false;
  } catch (error) {
    issueError = `Plane ${finding.fingerprint}: ${String(error.message || error).slice(0, 260)}`;
    errors.push(issueError);
  }
  const observedAt = isoNow();
  if (!await one('occurrences', 'occurrence_id', occurrenceId)) {
    await upsert('occurrences', 'occurrence_id', occurrenceId, {
      occurrence_id: occurrenceId, fingerprint: finding.fingerprint, report_date: date,
      project_id: finding.project_id, area: finding.area, failure_mode: finding.failure_mode,
      status: finding.status, severity: finding.severity, summary: finding.summary,
      evidence: JSON.stringify(finding.evidence), source_event_id: reportRow.source_event_id,
      ticket_key: ticketKey, observed_at: observedAt, event_sent: false,
    });
  }
  if (!previous || date >= previous.last_seen) {
    await upsert('findings', 'fingerprint', finding.fingerprint, {
      fingerprint: finding.fingerprint, project_id: finding.project_id, board_id: board.board_id,
      ticket_id: ticketId, ticket_key: ticketKey, active, first_seen: previous?.first_seen || date,
      last_seen: date, last_occurrence_id: occurrenceId, summary: finding.summary, area: finding.area,
      failure_mode: finding.failure_mode, severity: finding.severity, status: finding.status,
    });
  }
  handled.push({ ...finding, ticket_key: ticketKey, ticket_id: ticketId, error: issueError });
}

const links = handled.map((finding) => {
  const suffix = finding.error ? ` — pending: ${finding.error}` :
    finding.ticket_key ? ` — https://plane.delo.sh/33god/browse/${finding.ticket_key}/` :
    finding.status === 'resolved' ? ' — recorded as resolved' : ' — ticket pending';
  return `- ${finding.status.toUpperCase()} ${finding.summary}${suffix}`;
});
const appendix = ['## Dev Journal pipeline',
  `Report date: ${date}. Incident occurrences: ${handled.length}.`,
  ...links,
  errors.length ? '### Pending or failed steps' : '### Processing status',
  ...(errors.length ? errors.map((e) => `- ${e}`) : ['- All issue and notebook steps completed.'])].join('\n');
let finalMarkdown = `${content.markdown.trimEnd()}\n\n${appendix}\n`;
let noteId = reportRow.note_id || '';
try {
  const note = unwrap(await helpers.httpRequest({ method: 'PUT', json: true,
    url: `http://127.0.0.1:8775/v1/projects/infra/notes/daily/${date}`,
    headers: { 'Content-Type': 'application/json' },
    body: { title: `Dev Journal — ${date}`, content: finalMarkdown }, timeout: 30000 }));
  noteId = note.note_id || noteId;
  if (!noteId) throw new Error('Notebook adapter returned no note_id');
} catch (error) {
  errors.push(`Infra OpenNotebook pending: ${String(error.message || error).slice(0, 220)}`);
  finalMarkdown = `${finalMarkdown}\n- Infra OpenNotebook write pending.\n`;
}
const status = errors.length ? 'partial' : 'complete';
await upsert('reports', 'report_key', reportRow.report_key, {
  report_key: reportRow.report_key, status, note_id: noteId,
  errors: JSON.stringify(errors), processed_at: isoNow(), processing_lease_until: '',
  next_attempt_at: errors.length ? new Date(Date.now() + 10 * 60 * 1000).toISOString() : '',
});
return [{ json: { report_key: reportRow.report_key, report_date: date, backfill: reportRow.backfill,
  status, final_markdown: finalMarkdown, email_hash: digest(finalMarkdown),
  previous_email_hash: reportRow.email_hash || '', previous_email_id: reportRow.email_id || '',
  note_id: noteId, errors } }];
