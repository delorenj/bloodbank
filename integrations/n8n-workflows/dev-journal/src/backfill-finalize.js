const reports = await rows('reports');
const unfinished = reports.filter((row) => row.backfill === true && row.status !== 'complete');
if (unfinished.length) throw new Error(`${unfinished.length} backfilled daily reports are not fully processed`);
const unresolved = (await rows('findings')).filter((row) => row.status === 'open' && !row.ticket_id);
if (!unresolved.length) return [{ json: { created: 0, attached: 0 } }];
const registry = unwrap(await helpers.httpRequest({ method: 'GET',
  url: 'http://127.0.0.1:8764/v1/registry', json: true }));
const projects = registry.projects || {};
const infra = projects.infra?.ticket_provider;
if (!infra?.board_id || infra.workspace !== '33god' || infra.state !== 'linked') {
  throw new Error('Infra fallback board is unavailable');
}
async function plane(method, url, body) {
  const options = { method, url, json: true, headers: { 'User-Agent': 'Mozilla/5.0' } };
  if (body !== undefined) { options.body = body; options.headers['Content-Type'] = 'application/json'; }
  return unwrap(await helpers.httpRequestWithAuthentication('httpHeaderAuth', options));
}
async function pages(url) {
  const result = [], seen = new Set();
  for (let i = 0; url && i < 100; i++) {
    if (!url.startsWith('https://plane.delo.sh/api/v1/') || seen.has(url)) throw new Error('Unsafe Plane pagination');
    seen.add(url);
    const body = await plane('GET', url);
    result.push(...(Array.isArray(body) ? body : body.results || []));
    if (body.next) url = new URL(body.next, url).href;
    else if (body.next_page_results && body.next_cursor) {
      const next = new URL(url); next.searchParams.set('cursor', body.next_cursor); url = next.href;
    } else url = null;
  }
  if (url) throw new Error('Plane pagination limit exceeded');
  return result;
}
const boardCache = new Map();
async function boardInfo(id) {
  if (!boardCache.has(id)) {
    const base = `https://plane.delo.sh/api/v1/workspaces/33god/projects/${id}`;
    boardCache.set(id, { states: await pages(`${base}/states/`), issues: await pages(`${base}/issues/`) });
  }
  return boardCache.get(id);
}
let created = 0, attached = 0;
for (const finding of unresolved) {
  const source = projects[finding.project_id]?.ticket_provider;
  const linked = source?.type === 'plane' && source.state === 'linked' &&
    source.workspace === '33god' && source.board_id;
  const boardId = linked ? source.board_id : infra.board_id;
  const boardKey = linked ? source.identifier : infra.identifier || 'INFR';
  const info = await boardInfo(boardId);
  const inactive = new Set(info.states.filter((s) => ['completed', 'cancelled'].includes(s.group)).map((s) => s.id));
  const marker = `[dev-journal:${finding.fingerprint}]`;
  const matches = info.issues.filter((issue) => !inactive.has(issue.state) &&
    String(issue.description_html || '').includes(marker));
  if (matches.length > 1) throw new Error(`Ambiguous active ticket for ${finding.fingerprint}`);
  let issue = matches[0];
  if (!issue) {
    const backlog = info.states.find((state) => state.name === 'Backlog') ||
      info.states.find((state) => state.group === 'backlog');
    if (!backlog) throw new Error(`No Backlog state on board ${boardId}`);
    const occurrence = await one('occurrences', 'occurrence_id', finding.last_occurrence_id);
    const evidence = occurrence ? JSON.parse(occurrence.evidence || '[]') : [];
    const description = `<p>${escapeHtml(finding.summary)}</p>` +
      `<p>Unresolved in the verified Dev Journal archive. First seen ${finding.first_seen}; last seen ${finding.last_seen}. Source project: ${escapeHtml(finding.project_id)}.</p>` +
      `<ul>${evidence.map((line) => `<li>${escapeHtml(line)}</li>`).join('')}</ul>` +
      `<p>${escapeHtml(marker)}</p>`;
    issue = await plane('POST', `https://plane.delo.sh/api/v1/workspaces/33god/projects/${boardId}/issues/`, {
      name: finding.summary, state: backlog.id,
      priority: finding.severity === 'critical' ? 'urgent' : finding.severity,
      description_html: description,
    });
    info.issues.push(issue);
    created++;
  } else attached++;
  const ticketKey = issue.identifier || `${boardKey}-${issue.sequence_id}`;
  await upsert('findings', 'fingerprint', finding.fingerprint, {
    fingerprint: finding.fingerprint, board_id: boardId, ticket_id: issue.id,
    ticket_key: ticketKey, active: true,
  });
  if (finding.last_occurrence_id) await upsert('occurrences', 'occurrence_id', finding.last_occurrence_id, {
    occurrence_id: finding.last_occurrence_id, ticket_key: ticketKey,
  });
}
return [{ json: { created, attached, unresolved: unresolved.length } }];
