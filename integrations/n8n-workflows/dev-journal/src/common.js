// Shared by the custom Dev Journal node operations at build time.
async function rows(kind) { return helpers.journalRows(kind); }
async function one(kind, key, value) { return helpers.journalOne(kind, key, value); }
async function upsert(kind, key, value, data) {
  return helpers.journalUpsert(kind, key, value, data);
}
function nyDate(date = new Date()) {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York',
    year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
}
function isoNow() { return new Date().toISOString(); }
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}
function digest(value) {
  // Source signatures are change detectors, not cryptographic trust anchors.
  let h = 2166136261;
  for (const c of String(value)) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619); }
  return (h >>> 0).toString(16).padStart(8, '0');
}
function unwrap(response) {
  if (typeof response === 'string') return JSON.parse(response);
  return response;
}
