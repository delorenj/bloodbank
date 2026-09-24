// Shared by the custom Dev Journal node operations at build time.
async function rows(kind) { return helpers.journalRows(kind); }
async function one(kind, key, value) { return helpers.journalOne(kind, key, value); }
async function upsert(kind, key, value, data) {
  return helpers.journalUpsert(kind, key, value, data);
}
function slug(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}
function canonicalProject(value) {
  const name = slug(value);
  if (['infrastructure', 'delonet', 'delonet-infra', 'delonet-daily-report',
    'daily-report', 'reportctl', 'dev-journal'].includes(name)) return 'infra';
  if (['33god-pm-bak', '33god-pm-backup'].includes(name)) return '33god-pm';
  if (name === 'delodocs') return 'delodocs-pm';
  return name || 'infra';
}
function canonicalArea(value) {
  const name = slug(value);
  if (/^(report-delivery|daily-report-delivery|daily-reports|journal-delivery)$/.test(name))
    return 'report-delivery';
  if (/^(cron|cron-job|cron-jobs|cron-job-configuration|cron-scheduler)$/.test(name))
    return 'cron-jobs';
  if (/^(gateway|gateway-units|gateway-health|hermes-gateway-units)$/.test(name))
    return 'gateway-units';
  return name || 'unknown';
}
// These rules name repeatedly observed root problems, never the changing count,
// date, source-project guess, or phrasing used by an individual report.
function semanticCategory(issue) {
  const area = canonicalArea(issue.area);
  const description = `${issue.failure_mode || ''} ${issue.summary || ''}`.toLowerCase();
  const duplicateCron = /(?:33god-pm\.bak|shared cron dir|cron director(?:y|ies).*shar|duplicate.*(?:cron|job registration)|(?:cron|job).*duplicat|registered twice)/i.test(description);
  const missingSkills = /(?:delodocs-triage-second-pass|obsidian|llm-wiki)/i.test(description) &&
    /(?:missing|absent|not installed|uninstalled|lacks|required skills)/i.test(description);
  const gatewayDown = /\bgateway(?:s| units| services)?\b/i.test(description) &&
    /(?:not running|not active|inactive|unknown to systemd|down|unavailable|missing systemd|service not found)/i.test(description);
  const reportSubject = area === 'report-delivery' ||
    /(?:report delivery|(?:daily|nightly|published|verified|staged) reports?|report archive|archived report|delivered streak)/i.test(description);
  const reportFailure = /(?:missing|invalid|degraded|no valid|no published|no staged|skipped|delivery gap)/i.test(description);
  if (area === 'report-delivery' && reportFailure) return 'infra:report-delivery:missing-or-invalid-report';
  if (duplicateCron && missingSkills) return '';
  if (gatewayDown && (missingSkills || duplicateCron) && area !== 'gateway-units') return '';
  if (area === 'gateway-units' && gatewayDown) return 'infra:hermes-gateway:units-not-running';
  if (missingSkills) return 'delodocs-pm:cron-jobs:required-skills-missing';
  if (duplicateCron) return 'infra:cron-jobs:duplicate-profile-registration';
  if (gatewayDown) return 'infra:hermes-gateway:units-not-running';
  if (reportSubject && reportFailure) return 'infra:report-delivery:missing-or-invalid-report';
  return '';
}
function semanticFingerprint(issue) {
  const category = semanticCategory(issue);
  if (category) return category;
  const key = [canonicalProject(issue.project_id), canonicalArea(issue.area),
    slug(issue.failure_mode || issue.summary || 'requires-triage')].join(':');
  return key.length <= 220 ? key : `${key.slice(0, 195)}:${digest(key)}`;
}
function issueTokens(issue) {
  const words = slug(`${issue.failure_mode || ''} ${issue.summary || ''}`).split('-');
  const ignored = new Set(['with', 'from', 'that', 'this', 'were', 'have', 'been', 'report',
    'issue', 'daily', 'cron', 'jobs', 'status', 'error', 'missing', 'invalid']);
  return new Set(words.filter((word) => word.length >= 4 && !ignored.has(word) && !/^\d+$/.test(word)));
}
function validRecurrenceReference(issue, known) {
  if (!known) return false;
  const category = semanticCategory(issue), prior = semanticCategory(known);
  if (category || prior) return Boolean(category && category === prior);
  const tokens = issueTokens(issue), earlier = issueTokens(known);
  const shared = [...tokens].filter((token) => earlier.has(token)).length;
  const sameProject = canonicalProject(issue.project_id) === canonicalProject(known.project_id);
  const sameArea = canonicalArea(issue.area) === canonicalArea(known.area);
  return (sameProject && sameArea && shared >= 1) ||
    ((sameProject || sameArea) && shared >= 2) || shared >= 3;
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
