const processed = $input.first()?.json;
if (!processed || processed.backfill) return [];
const row = await one('reports', 'report_key', processed.report_key);
if (!row) throw new Error(`Missing report for email ${processed.report_key}`);
if (row.email_id && row.email_hash === processed.email_hash) return [];
const missed = await one('reports', 'report_key', `${processed.report_date}:missing`);
const isUpdate = Boolean(row.email_id || missed?.email_id);
const subject = `${isUpdate ? 'Updated: ' : ''}Dev Journal — ${processed.report_date}` +
  (processed.status === 'partial' ? ' (processing pending)' : '');
return [{ json: { report_key: processed.report_key, report_date: processed.report_date,
  email_hash: processed.email_hash,
  mail_key: `dev-journal-${processed.report_date}-${processed.email_hash}`,
  email: { from: 'Dev Journal <dev-journal@delo.sh>', to: ['jaradd@gmail.com'],
    subject, text: processed.final_markdown,
    html: `<pre style="font:14px/1.5 system-ui;white-space:pre-wrap">${escapeHtml(processed.final_markdown)}</pre>` },
} }];
