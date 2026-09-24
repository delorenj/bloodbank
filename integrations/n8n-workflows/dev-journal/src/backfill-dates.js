const run = $input.first().json || {};
if (run.exitCode !== 0) throw new Error(`Archive date enumeration failed: ${String(run.stderr || run.error).slice(0, 300)}`);
const dates = String(run.stdout || '').split(/\r?\n/).filter(Boolean);
const unique = new Set();
for (const date of dates) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || unique.has(date)) throw new Error(`Invalid or duplicate archive date ${date}`);
  unique.add(date);
}
return dates.map((date) => ({ json: { date } }));
