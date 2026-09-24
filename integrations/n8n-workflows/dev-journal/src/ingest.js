const out = [];
for (const item of $input.all()) {
  const raw = item.json || {};
  const backfill = raw.backfill === true;
  const data = backfill ? raw : raw.data;
  if (!backfill && (raw.type !== 'bloodbank.reporting.report.completed' ||
      raw.producer !== 'delonet-daily-report')) continue;
  const content = data?.content;
  const date = data?.report_date;
  const eventId = backfill ? data.source_event_id : raw.id;
  const runId = data?.run_id;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date || '') || !eventId || !runId ||
      !content || typeof content.markdown !== 'string' || !Array.isArray(content.collector_facts) ||
      !content.report || content.report.report_date !== date || content.report.run_id !== runId ||
      !/^[a-f0-9]{64}$/.test(content.content_sha256 || '') ||
      !content.generation_id || (backfill && data.content_sha256 !== content.content_sha256)) {
    throw new Error(`Invalid portable Dev Journal snapshot for ${date || 'unknown date'}`);
  }
  const reportKey = `${date}:${content.generation_id}`;
  const existing = await one('reports', 'report_key', reportKey);
  if (existing && (existing.content_sha256 !== content.content_sha256 ||
      existing.source_event_id !== eventId)) {
    throw new Error(`Immutable report generation changed: ${reportKey}`);
  }
  if (existing && !backfill && existing.backfill &&
      date === nyDate(new Date(Date.now() - 24 * 60 * 60 * 1000))) {
    await upsert('reports', 'report_key', reportKey, {
      report_key: reportKey, backfill: false, status: 'pending', next_attempt_at: '',
    });
  }
  if (!existing) await upsert('reports', 'report_key', reportKey, {
    report_key: reportKey,
    source_event_id: eventId,
    report_date: date,
    run_id: runId,
    generation_id: content.generation_id,
    content_sha256: content.content_sha256,
    payload: JSON.stringify(content),
    backfill,
    status: 'pending',
    note_id: '', email_id: '', email_hash: '', email_sent_at: '',
    processing_lease_until: '', next_attempt_at: '', attempts: 0,
    errors: '[]', processed_at: '', received_at: isoNow(),
  });
  out.push({ json: { report_key: reportKey, already_received: Boolean(existing), receipt: {
    source_event_id: eventId, report_date: date, run_id: runId,
    generation_id: content.generation_id, content_sha256: content.content_sha256,
    received_at: isoNow(),
  } } });
}
return out;
