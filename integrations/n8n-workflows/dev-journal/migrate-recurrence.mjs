#!/usr/bin/env node
// Consolidate historical wording-specific findings after verified backfill.
// Dry-run by default. Never replays reports, mail, incidents, or Plane writes.
import { existsSync, readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

const common = readFileSync(new URL('./src/common.js', import.meta.url), 'utf8');
const classify = new Function(`${common}\nreturn semanticCategory;`)();

function rank(row) {
  return Number(Boolean(row.active && row.ticket_id)) * 2 + Number(Boolean(row.ticket_id));
}

function plan(db, excluded = new Set()) {
  const findings = db.prepare('SELECT * FROM findings').all();
  const byCategory = new Map();
  for (const row of findings) {
    const semanticId = classify(row);
    if (!semanticId || excluded.has(row.fingerprint)) continue;
    if (!byCategory.has(semanticId)) byCategory.set(semanticId, []);
    byCategory.get(semanticId).push(row);
  }
  const groups = [];
  for (const [semanticId, members] of byCategory) {
    if (members.length < 2) continue;
    members.sort((a, b) => rank(b) - rank(a) ||
      String(a.first_seen || a.last_seen || '').localeCompare(String(b.first_seen || b.last_seen || '')) ||
      a.fingerprint.localeCompare(b.fingerprint));
    const anchor = members[0];
    const fingerprints = members.map((row) => row.fingerprint);
    const tickets = new Set(members.map((row) => row.ticket_id).filter(Boolean));
    const placeholders = fingerprints.map(() => '?').join(', ');
    const occurrenceCount = db.prepare(
      `SELECT COUNT(*) AS count FROM occurrences WHERE fingerprint IN (${placeholders})`,
    ).get(...fingerprints).count;
    groups.push({ semantic_id: semanticId, anchor: anchor.fingerprint,
      aliases: fingerprints.filter((fingerprint) => fingerprint !== anchor.fingerprint),
      finding_count: members.length, occurrence_count: occurrenceCount,
      ticket_key: anchor.ticket_key || '', conflict: tickets.size > 1 });
  }
  return groups.sort((a, b) => a.semantic_id.localeCompare(b.semantic_id));
}

function reviewedPlan(db, mapping) {
  if (!mapping) return { groups: [], triage: [] };
  if (mapping.schema_version !== 1 || !Array.isArray(mapping.groups) ||
      !Array.isArray(mapping.triage_fingerprints)) throw new Error('Invalid reviewed mapping schema');
  const findings = new Map(db.prepare('SELECT * FROM findings').all()
    .map((row) => [row.fingerprint, row]));
  const aliasTable = db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='recurrence_aliases'").get();
  const existingAlias = aliasTable ? db.prepare('SELECT canonical_fingerprint FROM recurrence_aliases WHERE legacy_fingerprint = ?') : null;
  const seen = new Set();
  const groups = [];
  for (const item of mapping.groups) {
    const members = [item.anchor, ...(item.aliases || [])];
    if (typeof item.semantic_id !== 'string' || !item.semantic_id.trim() ||
        members.length < 2 || members.some((value) => typeof value !== 'string' || !value)) {
      throw new Error('Invalid reviewed group');
    }
    for (const fingerprint of members) {
      if (seen.has(fingerprint)) throw new Error(`Overlapping reviewed groups: ${fingerprint}`);
      seen.add(fingerprint);
    }
    const anchor = findings.get(item.anchor);
    if (!anchor) throw new Error(`Reviewed anchor missing: ${item.anchor}`);
    const aliases = [];
    for (const fingerprint of item.aliases) {
      if (findings.has(fingerprint)) aliases.push(fingerprint);
      else if (existingAlias?.get(fingerprint)?.canonical_fingerprint !== item.anchor) {
        throw new Error(`Reviewed alias missing or remapped: ${fingerprint}`);
      }
    }
    if (!aliases.length) continue;
    const current = [anchor, ...aliases.map((fingerprint) => findings.get(fingerprint))];
    const tickets = new Set(current.map((row) => row.ticket_id).filter(Boolean));
    if (tickets.size && !anchor.ticket_id) {
      throw new Error(`Ticketed finding must be the reviewed anchor: ${item.anchor}`);
    }
    const placeholders = [item.anchor, ...aliases].map(() => '?').join(', ');
    const occurrenceCount = db.prepare(`SELECT COUNT(*) AS count FROM occurrences WHERE fingerprint IN (${placeholders})`)
      .get(item.anchor, ...aliases).count;
    groups.push({ semantic_id: item.semantic_id, anchor: item.anchor, aliases,
      finding_count: current.length, occurrence_count: occurrenceCount,
      ticket_key: anchor.ticket_key || current.find((row) => row.ticket_key)?.ticket_key || '',
      conflict: tickets.size > 1 });
  }
  const triage = [];
  for (const fingerprint of mapping.triage_fingerprints) {
    const row = findings.get(fingerprint);
    if (!row) throw new Error(`Reviewed triage finding missing: ${fingerprint}`);
    if (seen.has(fingerprint)) throw new Error(`Reviewed triage overlaps a merge group: ${fingerprint}`);
    if (row.ticket_id || row.ticket_key) throw new Error(`Ticketed finding cannot be demoted to triage: ${fingerprint}`);
    if (!row.summary?.startsWith('Triage Dev Journal finding:')) {
      throw new Error(`Reviewed triage fingerprint is not a fallback: ${fingerprint}`);
    }
    if (row.status !== 'triage') triage.push(fingerprint);
  }
  return { groups, triage };
}

function apply(db, groups, triage) {
  const unfinished = db.prepare(
    "SELECT COUNT(*) AS count FROM reports WHERE backfill = 1 AND status != 'complete'",
  ).get().count;
  if (unfinished) throw new Error(`${unfinished} backfill reports are unfinished; migration refused`);
  const conflict = groups.find((group) => group.conflict);
  if (conflict) throw new Error(`Multiple Plane tickets in ${conflict.semantic_id}; resolve aliases first`);
  db.exec(`CREATE TABLE IF NOT EXISTS recurrence_aliases (
    legacy_fingerprint TEXT PRIMARY KEY,
    canonical_fingerprint TEXT NOT NULL,
    semantic_id TEXT NOT NULL,
    migrated_at TEXT NOT NULL
  )`);
  const alias = db.prepare(`INSERT INTO recurrence_aliases
    (legacy_fingerprint, canonical_fingerprint, semantic_id, migrated_at) VALUES (?, ?, ?, ?)
    ON CONFLICT(legacy_fingerprint) DO UPDATE SET
      canonical_fingerprint = excluded.canonical_fingerprint,
      semantic_id = excluded.semantic_id`);
  const moveOccurrences = db.prepare(`UPDATE occurrences SET fingerprint = ?,
    ticket_key = CASE WHEN ? != '' THEN ? ELSE ticket_key END WHERE fingerprint = ?`);
  const removeFinding = db.prepare('DELETE FROM findings WHERE fingerprint = ?');
  const updateAnchor = db.prepare(`UPDATE findings SET first_seen = ?, last_seen = ?,
    last_occurrence_id = ?, status = ? WHERE fingerprint = ?`);
  const stamped = new Date().toISOString();
  for (const group of groups) {
    const members = [group.anchor, ...group.aliases];
    const placeholders = members.map(() => '?').join(', ');
    const rows = db.prepare(`SELECT first_seen, last_seen, status FROM findings
      WHERE fingerprint IN (${placeholders})`).all(...members);
    const dates = rows.map((row) => row.first_seen).filter(Boolean).sort();
    const latest = rows.sort((a, b) => String(b.last_seen || '').localeCompare(String(a.last_seen || '')) ||
      Number(b.status === 'open') - Number(a.status === 'open'))[0];
    const occurrence = db.prepare(`SELECT occurrence_id FROM occurrences
      WHERE fingerprint IN (${placeholders}) ORDER BY report_date DESC, observed_at DESC LIMIT 1`)
      .get(...members);
    for (const old of group.aliases) {
      alias.run(old, group.anchor, group.semantic_id, stamped);
      moveOccurrences.run(group.anchor, group.ticket_key, group.ticket_key, old);
      removeFinding.run(old);
    }
    updateAnchor.run(dates[0] || '', latest.last_seen || '', occurrence?.occurrence_id || '',
      latest.status || 'open', group.anchor);
  }
  const demoteFinding = db.prepare("UPDATE findings SET status = 'triage', active = 0 WHERE fingerprint = ?");
  const demoteOccurrences = db.prepare("UPDATE occurrences SET status = 'triage' WHERE fingerprint = ?");
  for (const fingerprint of triage) {
    demoteFinding.run(fingerprint);
    demoteOccurrences.run(fingerprint);
  }
}

function main(argv) {
  if (argv.includes('--help')) {
    console.log('Usage: node migrate-recurrence.mjs --db PATH [--mapping PATH] [--apply]');
    return;
  }
  const index = argv.indexOf('--db');
  const mappingIndex = argv.indexOf('--mapping');
  if (index < 0 || !argv[index + 1] || (mappingIndex >= 0 && !argv[mappingIndex + 1]) ||
      argv.some((arg, i) => !['--db', '--mapping', '--apply'].includes(arg) &&
        i !== index + 1 && i !== mappingIndex + 1)) {
    throw new Error('Usage: node migrate-recurrence.mjs --db PATH [--mapping PATH] [--apply]');
  }
  const path = argv[index + 1];
  if (!existsSync(path)) throw new Error(`Journal database does not exist: ${path}`);
  const applying = argv.includes('--apply');
  const mapping = mappingIndex < 0 ? null : JSON.parse(readFileSync(argv[mappingIndex + 1], 'utf8'));
  const db = new DatabaseSync(path, { readOnly: !applying });
  try {
    db.exec('PRAGMA busy_timeout = 5000');
    if (applying) db.exec('BEGIN IMMEDIATE');
    try {
      const reviewed = reviewedPlan(db, mapping);
      const reviewedMembers = new Set(mapping?.groups.flatMap((group) => [group.anchor, ...group.aliases]) || []);
      const groups = [...reviewed.groups, ...plan(db, reviewedMembers)];
      if (applying) apply(db, groups, reviewed.triage);
      if (applying) db.exec('COMMIT');
      console.log(JSON.stringify({ mode: applying ? 'applied' : 'dry-run',
        groups: groups.length, aliases: groups.reduce((n, group) => n + group.aliases.length, 0),
        occurrences: groups.reduce((n, group) => n + group.occurrence_count, 0),
        triage_findings: reviewed.triage.length,
        details: groups }, null, 2));
    } catch (error) {
      if (applying) db.exec('ROLLBACK');
      throw error;
    }
  } finally { db.close(); }
}

main(process.argv.slice(2));
