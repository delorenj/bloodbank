const date = nyDate(new Date(Date.now() - 24 * 60 * 60 * 1000));
const candidates = (await rows('reports')).filter((row) => row.report_date === date && row.generation_id !== 'missing');
candidates.sort((a, b) => String(b.received_at).localeCompare(String(a.received_at)));
let report = candidates[0];
if (!report) {
  const reportKey = `${date}:missing`;
  report = await one('reports', 'report_key', reportKey);
  if (!report) report = await upsert('reports', 'report_key', reportKey, {
    report_key: reportKey, source_event_id: `missing:${date}`, report_date: date,
    run_id: '', generation_id: 'missing', content_sha256: '', payload: '{}',
    backfill: false, status: 'missing', note_id: '', email_id: '', email_hash: '',
    email_sent_at: '', processing_lease_until: '', next_attempt_at: '', attempts: 0,
    errors: '["No verified published Dev Journal was available by 07:00."]',
    processed_at: '', received_at: isoNow(),
  });
}
if (report.email_id) return [];
if (report.backfill) await upsert('reports', 'report_key', report.report_key, {
  report_key: report.report_key, backfill: false, status: 'pending',
});
const missing = report.generation_id === 'missing';
const content = missing ? { markdown: `# Dev Journal unavailable — ${date}\n\nNo verified published Dev Journal was available by 07:00 America/New_York.`,
  report: {} } : JSON.parse(report.payload);
const occurrences = (await rows('occurrences')).filter((row) => row.report_date === date);
const pending = JSON.parse(report.errors || '[]');
const incidentLines = occurrences.map((row) =>
  `- ${row.status.toUpperCase()} ${row.summary}${row.ticket_key ? ` — https://plane.delo.sh/33god/browse/${row.ticket_key}/` : ' — ticket pending'}`);
const suffix = ['## Dev Journal pipeline — 07:00 processing status',
  `Processing is ${missing ? 'missing' : report.status}; ${occurrences.length} issue(s) have been recorded so far.`,
  ...incidentLines,
  '### Pending or failed steps',
  ...(pending.length ? pending.map((error) => `- ${error}`) : ['- Issue extraction or delivery has not finished.']),
  '- The pipeline will retry incomplete work and send a clearly marked update when it changes.'].join('\n');
const text = `${content.markdown.trimEnd()}\n\n${suffix}\n`;
let noteId = report.note_id || '';
try {
  const note = unwrap(await helpers.httpRequest({ method: 'PUT', json: true,
    url: `http://127.0.0.1:8775/v1/projects/infra/notes/daily/${date}`,
    headers: { 'Content-Type': 'application/json' },
    body: { title: `Dev Journal — ${date}`, content: text }, timeout: 30000 }));
  noteId = note.note_id || noteId;
} catch (error) { pending.push(`Infra OpenNotebook pending: ${String(error.message || error).slice(0, 200)}`); }
await upsert('reports', 'report_key', report.report_key, { report_key: report.report_key,
  status: missing ? 'missing' : 'partial', note_id: noteId, errors: JSON.stringify(pending) });
const hash = digest(text);
return [{ json: { report_key: report.report_key, report_date: date,
  email_hash: hash, mail_key: `dev-journal-${date}-${hash}`,
  email: { from: 'Dev Journal <dev-journal@delo.sh>', to: ['jaradd@gmail.com'],
    subject: `Dev Journal — ${date} (${missing ? 'report missing' : 'processing pending'})`, text,
    html: `<pre style="font:14px/1.5 system-ui;white-space:pre-wrap">${escapeHtml(text)}</pre>` },
} }];
