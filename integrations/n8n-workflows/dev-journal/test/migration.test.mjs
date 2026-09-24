import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';

const script = new URL('../migrate-recurrence.mjs', import.meta.url).pathname;

function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'journal-recurrence-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const path = join(directory, 'state.sqlite');
  const db = new DatabaseSync(path);
  db.exec(`
    CREATE TABLE reports (backfill INTEGER, status TEXT);
    CREATE TABLE findings (fingerprint TEXT PRIMARY KEY, project_id TEXT, area TEXT,
      failure_mode TEXT, summary TEXT, active INTEGER, ticket_id TEXT, ticket_key TEXT,
      first_seen TEXT, last_seen TEXT, last_occurrence_id TEXT, status TEXT);
    CREATE TABLE occurrences (occurrence_id TEXT PRIMARY KEY, fingerprint TEXT,
      report_date TEXT, observed_at TEXT, ticket_key TEXT, event_sent INTEGER);
    INSERT INTO reports VALUES (1, 'complete');
    INSERT INTO findings VALUES
      ('infra:report-delivery:old-wording', 'infra', 'report delivery',
       'Missing valid published report', 'Report delivery degraded by missing days',
       1, 'issue-5', 'INFR-5', '2026-09-23', '2026-09-23', '2026-09-23:old', 'open'),
      ('delonet-daily-report:daily-report:missing', 'delonet-daily-report', 'report delivery',
       'Missing and invalid daily reports', 'Archived reports are missing or invalid',
       0, '', '', '2026-08-18', '2026-08-18', '2026-08-18:old', 'open');
    INSERT INTO occurrences VALUES
      ('2026-09-23:old', 'infra:report-delivery:old-wording', '2026-09-23', '2026-09-23T12:00:00Z', 'INFR-5', 1),
      ('2026-08-18:old', 'delonet-daily-report:daily-report:missing', '2026-08-18', '2026-08-18T12:00:00Z', '', 0);
  `);
  return { db, path };
}

function run(path, apply = false) {
  return spawnSync(process.execPath, [script, '--db', path, ...(apply ? ['--apply'] : [])], {
    encoding: 'utf8',
  });
}

test('dry-run plans aliases; apply preserves occurrence identity and publication state', (t) => {
  const { db, path } = fixture(t);
  const preview = run(path);
  assert.equal(preview.status, 0, preview.stderr);
  const plan = JSON.parse(preview.stdout);
  assert.equal(plan.groups, 1);
  assert.equal(plan.aliases, 1);
  assert.equal(plan.occurrences, 2);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM findings').get().count, 2);
  assert.equal(db.prepare("SELECT COUNT(*) AS count FROM sqlite_master WHERE name='recurrence_aliases'").get().count, 0);

  const applied = run(path, true);
  assert.equal(applied.status, 0, applied.stderr);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM findings').get().count, 1);
  const occurrences = db.prepare('SELECT occurrence_id, fingerprint, ticket_key, event_sent FROM occurrences ORDER BY report_date').all();
  assert.deepEqual(occurrences.map((row) => row.occurrence_id), ['2026-08-18:old', '2026-09-23:old']);
  assert.ok(occurrences.every((row) => row.fingerprint === 'infra:report-delivery:old-wording'));
  assert.deepEqual(occurrences.map((row) => row.event_sent), [0, 1]);
  assert.ok(occurrences.every((row) => row.ticket_key === 'INFR-5'));
  const alias = db.prepare('SELECT canonical_fingerprint, semantic_id FROM recurrence_aliases').get();
  assert.equal(alias.canonical_fingerprint, 'infra:report-delivery:old-wording');
  assert.equal(alias.semantic_id, 'infra:report-delivery:missing-or-invalid-report');
  assert.equal(JSON.parse(run(path, true).stdout).groups, 0);
  db.close();
});

test('migration refuses unfinished backfill before changing any rows', (t) => {
  const { db, path } = fixture(t);
  db.exec("UPDATE reports SET status='pending'");
  const result = run(path, true);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /backfill reports are unfinished/);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM findings').get().count, 2);
  assert.equal(db.prepare("SELECT COUNT(*) AS count FROM sqlite_master WHERE name='recurrence_aliases'").get().count, 0);
  db.close();
});
