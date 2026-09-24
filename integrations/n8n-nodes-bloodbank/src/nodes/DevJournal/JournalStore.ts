import { chmodSync, mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

type Kind = 'reports' | 'findings' | 'occurrences' | 'rollups';
type JournalRow = Record<string, unknown>;

const columns: Record<Kind, Record<string, string>> = {
  reports: {
    report_key: 'TEXT PRIMARY KEY', source_event_id: 'TEXT', report_date: 'TEXT', run_id: 'TEXT',
    generation_id: 'TEXT', content_sha256: 'TEXT', payload: 'TEXT', backfill: 'INTEGER',
    status: 'TEXT', note_id: 'TEXT', email_id: 'TEXT', email_hash: 'TEXT', email_sent_at: 'TEXT',
    processing_lease_until: 'TEXT', next_attempt_at: 'TEXT', attempts: 'INTEGER',
    errors: 'TEXT', processed_at: 'TEXT', received_at: 'TEXT',
  },
  findings: {
    fingerprint: 'TEXT PRIMARY KEY', project_id: 'TEXT', board_id: 'TEXT', ticket_id: 'TEXT',
    ticket_key: 'TEXT', active: 'INTEGER', first_seen: 'TEXT', last_seen: 'TEXT',
    last_occurrence_id: 'TEXT', summary: 'TEXT', area: 'TEXT', failure_mode: 'TEXT',
    severity: 'TEXT', status: 'TEXT',
  },
  occurrences: {
    occurrence_id: 'TEXT PRIMARY KEY', fingerprint: 'TEXT', report_date: 'TEXT',
    project_id: 'TEXT', area: 'TEXT', failure_mode: 'TEXT', status: 'TEXT', severity: 'TEXT',
    summary: 'TEXT', evidence: 'TEXT', source_event_id: 'TEXT', ticket_key: 'TEXT',
    observed_at: 'TEXT', event_sent: 'INTEGER',
  },
  rollups: {
    period_key: 'TEXT PRIMARY KEY', kind: 'TEXT', start_date: 'TEXT', end_date: 'TEXT',
    source_signature: 'TEXT', content: 'TEXT', day_slices: 'TEXT', note_id: 'TEXT',
    updated_at: 'TEXT',
  },
};

const primaryKeys: Record<Kind, string> = {
  reports: 'report_key', findings: 'fingerprint',
  occurrences: 'occurrence_id', rollups: 'period_key',
};
const booleans = new Set(['backfill', 'active', 'event_sent']);

function checkKind(kind: string): Kind {
  if (!(kind in columns)) throw new Error(`Unknown journal table ${kind}`);
  return kind as Kind;
}

function rowFromSql(row: JournalRow | undefined): JournalRow | null {
  if (!row) return null;
  const result = { ...row };
  for (const field of booleans) {
    if (result[field] !== null && result[field] !== undefined) result[field] = result[field] === 1;
  }
  return result;
}

/** Durable, queryable journal state. All writes merge one keyed row in a SQLite transaction. */
export class JournalStore {
  private readonly db: DatabaseSync;

  constructor(path = join(homedir(), '.local', 'state', 'dev-journal', 'state.sqlite')) {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(path);
    chmodSync(path, 0o600);
    this.db.exec('PRAGMA journal_mode = WAL; PRAGMA synchronous = FULL; PRAGMA busy_timeout = 5000;');
    for (const [kind, fields] of Object.entries(columns)) {
      this.db.exec(`CREATE TABLE IF NOT EXISTS ${kind} (${Object.entries(fields)
        .map(([name, type]) => `${name} ${type}`).join(', ')})`);
    }
    this.db.exec(`
      CREATE INDEX IF NOT EXISTS reports_by_date ON reports(report_date);
      CREATE INDEX IF NOT EXISTS occurrences_by_date ON occurrences(report_date);
      CREATE INDEX IF NOT EXISTS occurrences_by_fingerprint ON occurrences(fingerprint);
      CREATE INDEX IF NOT EXISTS findings_by_last_seen ON findings(last_seen);
      CREATE INDEX IF NOT EXISTS rollups_by_kind_start ON rollups(kind, start_date);
    `);
  }

  rows(kindInput: string): JournalRow[] {
    const kind = checkKind(kindInput);
    return this.db.prepare(`SELECT * FROM ${kind}`).all().map((row) => rowFromSql(row) as JournalRow);
  }

  one(kindInput: string, field: string, value: string): JournalRow | null {
    const kind = checkKind(kindInput);
    if (!(field in columns[kind])) throw new Error(`Unknown ${kind} column ${field}`);
    return rowFromSql(this.db.prepare(`SELECT * FROM ${kind} WHERE ${field} = ?`).get(value));
  }

  upsert(kindInput: string, field: string, value: string, data: JournalRow): JournalRow {
    const kind = checkKind(kindInput);
    if (field !== primaryKeys[kind]) throw new Error(`${kind} must be upserted by ${primaryKeys[kind]}`);
    if (!value || (data[field] !== undefined && data[field] !== value)) {
      throw new Error(`Invalid ${kind} key ${field}`);
    }
    const update = { ...data, [field]: value };
    const names = Object.keys(update);
    for (const name of names) {
      if (!(name in columns[kind])) throw new Error(`Unknown ${kind} column ${name}`);
    }
    const values: (string | number | bigint | null)[] = names.map((name) => {
      const item = update[name];
      if (item === undefined) throw new Error(`Undefined ${kind}.${name}`);
      if (booleans.has(name)) return item === null ? null : item ? 1 : 0;
      if (item === null || typeof item === 'string' || typeof item === 'number' ||
          typeof item === 'bigint') return item;
      throw new Error(`Unsupported ${kind}.${name} value`);
    });
    const assignments = names.filter((name) => name !== field)
      .map((name) => `${name} = excluded.${name}`).join(', ');
    const statement = `INSERT INTO ${kind} (${names.join(', ')}) VALUES (${names.map(() => '?').join(', ')}) ` +
      `ON CONFLICT(${field}) DO UPDATE SET ${assignments || `${field} = excluded.${field}`}`;
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare(statement).run(...values);
      const row = this.one(kind, field, value);
      this.db.exec('COMMIT');
      if (!row) throw new Error(`${kind} ${value} disappeared after upsert`);
      return row;
    } catch (error) {
      this.db.exec('ROLLBACK');
      throw error;
    }
  }

  close(): void { this.db.close(); }
}

let singleton: JournalStore | undefined;
let singletonPath: string | undefined;
export function journalStore(): JournalStore {
  const path = process.env.DEV_JOURNAL_DB_PATH ||
    join(homedir(), '.local', 'state', 'dev-journal', 'state.sqlite');
  if (singletonPath !== path) {
    singleton?.close();
    singleton = new JournalStore(path);
    singletonPath = path;
  }
  return singleton!;
}
