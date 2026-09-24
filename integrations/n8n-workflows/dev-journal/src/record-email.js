const mail = ctx.mail;
if (!mail) throw new Error('No prepared journal mail in this execution');
const response = $input.first().json || {};
const row = await one('reports', 'report_key', mail.report_key);
if (!row) throw new Error(`Report disappeared after email: ${mail.report_key}`);
if (response.error || !response.id) {
  const reason = `Resend pending: ${JSON.stringify(response.error || response).slice(0, 240)}`;
  const errors = [...new Set([...(JSON.parse(row.errors || '[]')), reason])];
  await upsert('reports', 'report_key', mail.report_key, { report_key: mail.report_key,
    status: 'partial', errors: JSON.stringify(errors),
    next_attempt_at: new Date(Date.now() + 10 * 60 * 1000).toISOString() });
  return [{ json: { report_key: mail.report_key, delivered: false, reason } }];
}
await upsert('reports', 'report_key', mail.report_key, { report_key: mail.report_key,
  email_id: response.id, email_hash: mail.email_hash, email_sent_at: isoNow() });
return [{ json: { report_key: mail.report_key, delivered: true, resend_id: response.id } }];
