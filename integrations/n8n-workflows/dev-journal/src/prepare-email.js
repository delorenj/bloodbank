const processed = $input.first()?.json;
if (!processed || processed.backfill) return [];
const row = await one('reports', 'report_key', processed.report_key);
if (!row) throw new Error(`Missing report for email ${processed.report_key}`);
const pending = await pendingMail(row);
if (pending) return pending;
const phase = processed.status === 'complete' ? 'complete' : 'partial';
// Any historical receipt consumes the partial slot, including pre-v2 hashes.
if (row.email_id && (phase === 'partial' || row.email_phase === 'complete' ||
    row.email_hash === mailIdentity(row, 'complete'))) return [];
const missed = await one('reports', 'report_key', `${processed.report_date}:missing`);
const isUpdate = Boolean(row.email_id || missed?.email_id);
const subject = `${isUpdate ? 'Updated: ' : ''}Dev Journal — ${processed.report_date}` +
  (phase === 'partial' ? ' (processing pending)' : '');
return stageMail(row, phase, subject, processed.final_markdown);
