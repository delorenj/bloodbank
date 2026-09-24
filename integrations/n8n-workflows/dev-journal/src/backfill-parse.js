const seen = new Set();
return $input.all().map((item) => {
  const run = item.json || {};
  if (run.exitCode !== 0) throw new Error(`Snapshot export failed: ${String(run.stderr || run.error).slice(0, 500)}`);
  const snapshot = JSON.parse(run.stdout);
  const key = `${snapshot.report_date}:${snapshot.generation_id}`;
  if (seen.has(key)) throw new Error(`Duplicate archive snapshot ${key}`);
  seen.add(key);
  return { json: { ...snapshot, backfill: true } };
});
