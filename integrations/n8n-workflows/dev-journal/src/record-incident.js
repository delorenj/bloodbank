const published = $input.all();
const pending = ctx.incidents;
if (published.length !== pending.length) throw new Error('Incident publication count changed');
for (let i = 0; i < pending.length; i++) {
  const id = pending[i].json.occurrence_id;
  await upsert('occurrences', 'occurrence_id', id, { occurrence_id: id, event_sent: true });
}
return pending.map((item) => ({ json: { occurrence_id: item.json.occurrence_id, event_sent: true } }));
