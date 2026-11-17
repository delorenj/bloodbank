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
    email_phase: 'TEXT', pending_email: 'TEXT', email_receipts: 'TEXT',
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
    this.db.exec('PRAGMA busy_timeout = 5000; PRAGMA journal_mode = WAL; PRAGMA synchronous = FULL;');
    this.mailTransaction(() => {
      for (const [kind, fields] of Object.entries(columns)) {
        this.db.exec(`CREATE TABLE IF NOT EXISTS ${kind} (${Object.entries(fields)
          .map(([name, type]) => `${name} ${type}`).join(', ')})`);
      }
      const reportColumns = new Set(this.db.prepare('PRAGMA table_info(reports)').all()
        .map((column) => column.name));
      for (const name of ['email_phase', 'pending_email', 'email_receipts']) {
        if (!reportColumns.has(name)) this.db.exec(`ALTER TABLE reports ADD COLUMN ${name} TEXT`);
      }
      // Infer old receipts before processing can overwrite status; never remove history.
      this.db.exec(`UPDATE reports SET email_phase = CASE WHEN status = 'complete'
        THEN 'complete' ELSE 'partial' END
        WHERE email_id IS NOT NULL AND email_id != '' AND email_phase IS NULL`);
      this.db.exec(`
        CREATE INDEX IF NOT EXISTS reports_by_date ON reports(report_date);
        CREATE INDEX IF NOT EXISTS occurrences_by_date ON occurrences(report_date);
        CREATE INDEX IF NOT EXISTS occurrences_by_fingerprint ON occurrences(fingerprint);
        CREATE INDEX IF NOT EXISTS findings_by_last_seen ON findings(last_seen);
        CREATE INDEX IF NOT EXISTS rollups_by_kind_start ON rollups(kind, start_date);
      `);
    });
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

  /** Serialize the decision and persisted bytes, not just the subsequent write. */
  reserveMail(reportKey: string, candidate: JournalRow | null): JournalRow | null {
    return this.mailTransaction(() => {
      const row = this.one('reports', 'report_key', reportKey);
      if (!row) throw new Error(`Missing mail report ${reportKey}`);
      if (row.pending_email) return JSON.parse(String(row.pending_email)) as JournalRow;
      if (!candidate) return null;
      const phase = candidate.email_phase;
      const identity = `dev-journal-v2:${reportKey}:${row.content_sha256 || ''}:${phase}`;
      if (!['partial', 'complete'].includes(String(phase)) || candidate.report_key !== reportKey ||
          candidate.mail_key !== identity || candidate.email_hash !== identity) {
        throw new Error('Invalid journal mail identity');
      }
      // Historical receipts consume partial; completion permanently closes both slots.
      if (row.email_id && (phase === 'partial' || row.email_phase === 'complete' ||
          row.email_hash === `dev-journal-v2:${reportKey}:${row.content_sha256 || ''}:complete`)) return null;
      this.db.prepare('UPDATE reports SET pending_email = ? WHERE report_key = ?')
        .run(JSON.stringify(candidate), reportKey);
      return candidate;
    });
  }

  /** Only the exact frozen reservation may settle or schedule its retry. */
  settleMail(mail: JournalRow, receiptId: string | null, reason = ''): boolean {
    return this.mailTransaction(() => {
      const row = this.one('reports', 'report_key', String(mail.report_key));
      if (!row) throw new Error(`Missing mail report ${mail.report_key}`);
      if (!row.pending_email || JSON.stringify(JSON.parse(String(row.pending_email))) !== JSON.stringify(mail)) {
        // Repeated acknowledgement of an already recorded provider receipt is a no-op success.
        const receipts = JSON.parse(String(row.email_receipts || '[]')) as JournalRow[];
        return Boolean(receiptId && receipts.some((item) => item.id === receiptId &&
          item.key === mail.mail_key && item.phase === mail.email_phase));
      }
      if (!receiptId) {
        const errors = [...new Set([...JSON.parse(String(row.errors || '[]')), reason])];
        this.db.prepare('UPDATE reports SET errors = ?, next_attempt_at = ? WHERE report_key = ?')
          .run(JSON.stringify(errors), new Date(Date.now() + 10 * 60 * 1000).toISOString(), String(mail.report_key));
        return true;
      }
      const receipts = JSON.parse(String(row.email_receipts || '[]')) as JournalRow[];
      // Preserve the pre-migration receipt before replacing the legacy summary fields.
      if (row.email_id && !receipts.some((item) => item.id === row.email_id)) {
        receipts.push({ id: row.email_id, key: row.email_hash, phase: row.email_phase, at: row.email_sent_at });
      }
      const at = new Date().toISOString();
      receipts.push({ id: receiptId, key: mail.mail_key, phase: mail.email_phase, at });
      this.db.prepare(`UPDATE reports SET email_id = ?, email_hash = ?, email_phase = ?,
        email_sent_at = ?, pending_email = '', email_receipts = ? WHERE report_key = ?`)
        .run(receiptId, String(mail.email_hash), String(mail.email_phase), at,
          JSON.stringify(receipts), String(mail.report_key));
      return true;
    });
  }

  private mailTransaction<T>(operation: () => T): T {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = operation();
      this.db.exec('COMMIT');
      return result;
    } catch (error) {
      this.db.exec('ROLLBACK');
      throw error;
    }
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
      if (kind === 'reports' && this.one(kind, field, value)) {
        const protectedFields = ['email_id', 'email_hash', 'email_phase', 'email_sent_at', 'pending_email', 'email_receipts'];
        if (names.some((name) => protectedFields.includes(name))) {
          throw new Error('Existing mail state requires reserveMail/settleMail');
        }
      }
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

  close(): void { if (this.db.isOpen) this.db.close(); }
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
