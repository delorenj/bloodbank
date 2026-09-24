const pending = (await rows('occurrences')).filter((row) =>
  ['open', 'resolved'].includes(row.status) && row.event_sent !== true)
  .sort((a, b) => a.report_date.localeCompare(b.report_date)).slice(0, 100);
return pending.map((row) => ({ json: { occurrence_id: row.occurrence_id,
  event: { occurrence_id: row.occurrence_id, fingerprint: row.fingerprint,
    report_date: row.report_date, project_id: row.project_id, area: row.area,
    failure_mode: row.failure_mode, status: row.status, severity: row.severity,
    summary: row.summary, evidence: JSON.parse(row.evidence || '[]'),
    source_event_id: row.source_event_id, observed_at: row.observed_at,
    ...(row.ticket_key ? { ticket_key: row.ticket_key } : {}) },
} }));
