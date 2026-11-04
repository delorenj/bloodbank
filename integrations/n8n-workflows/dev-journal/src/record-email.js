const mail = ctx.mail;
if (!mail) throw new Error('No prepared journal mail in this execution');
const response = $input.first().json || {};
if (response.error || !response.id) {
  const reason = `Resend pending: ${JSON.stringify(response.error || response).slice(0, 240)}`;
  const owned = await helpers.journalSettleMail(mail, null, reason);
  return [{ json: { report_key: mail.report_key, delivered: false, reason, stale: !owned,
    candybar: { status: 'fail', message: reason } } }];
}
const owned = await helpers.journalSettleMail(mail, response.id, '');
if (!owned) return [{ json: { report_key: mail.report_key, delivered: false, stale: true,
  candybar: { status: 'fail', message: 'Stale journal mail receipt; current reservation preserved' } } }];
return [{ json: { report_key: mail.report_key, delivered: true, resend_id: response.id,
  candybar: { status: 'success' } } }];
