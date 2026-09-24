// Shared by the custom Dev Journal n8n node operations at build time.
// The node uses n8n's in-process Data Table proxies; no API token or table ID is embedded.
const TABLE_PREFIX = 'dev_journal_';
async function table(kind) {
  const name = `${TABLE_PREFIX}${kind}`;
  const aggregate = await helpers.getDataTableAggregateProxy();
  const result = await aggregate.getManyAndCount({ filter: { name }, take: 2 });
  const rows = (result.data || []).filter((row) => row.name === name);
  if (rows.length !== 1) throw new Error(`Data Table ${name} is missing or ambiguous; run setup`);
  return helpers.getDataTableProxy(rows[0].id);
}
async function rows(kind, filter = { type: 'and', filters: [] }) {
  const proxy = await table(kind);
  const out = [];
  for (let skip = 0; skip < 100000; skip += 1000) {
    const page = await proxy.getManyRowsAndCount({ skip, take: 1000, filter });
    out.push(...(page.data || []));
    if (out.length >= page.count || (page.data || []).length === 0) return out;
  }
  throw new Error(`Data Table ${kind} pagination exceeded 100000 rows`);
}
function eq(columnName, value) {
  return { type: 'and', filters: [{ columnName, condition: 'eq', value }] };
}
async function one(kind, key, value) {
  const hits = await rows(kind, eq(key, value));
  if (hits.length > 1) throw new Error(`Duplicate ${kind} rows for ${key}=${value}`);
  return hits[0] || null;
}
async function upsert(kind, key, value, data) {
  const proxy = await table(kind);
  const out = await proxy.upsertRow({ data, filter: eq(key, value), dryRun: false });
  if (!out.length) throw new Error(`Upsert ${kind} ${value} returned no row`);
  return out[0];
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
