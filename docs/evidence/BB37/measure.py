#!/usr/bin/env python3
"""Read-only BB-37 receipt measurement. No prompts, payloads, or environment values."""
import argparse
import datetime as dt
import json
import sqlite3
import urllib.request
from pathlib import Path

IDS = ('codegraph-prompt', 'code-review-graph-status', 'code-review-graph-update',
       'claude-notify', 'codex-turn-notify')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=Path(__file__).with_name('baseline.json'))
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text())
    begin, end = baseline['window_start_utc'], baseline['window_end_utc']
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    db = Path.home() / '.local/state/33god/hook-hub/receipts.sqlite3'
    conn = sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    measured = {}
    for handler in IDS:
        rows = [dict(row) for row in conn.execute('''
            SELECT e.status, COALESCE(e.reason,'') AS reason, count(*) AS count
            FROM executions e JOIN invocations i USING(invocation_id)
            WHERE e.handler_id=? AND i.received_at>=? AND i.received_at<?
            GROUP BY e.status,e.reason ORDER BY e.status,e.reason''', (handler, begin, end))]
        latest = conn.execute('''
            SELECT i.received_at, e.rowid AS receipt_seq FROM executions e
            JOIN invocations i USING(invocation_id)
            WHERE e.handler_id=? AND i.received_at>=? AND i.received_at<?
              AND e.reason='handler_binary_missing'
            ORDER BY i.received_at DESC LIMIT 1''', (handler, begin, end)).fetchone()
        measured[handler] = {'count': sum(row['count'] for row in rows),
                             'status_reason_counts': rows,
                             'latest_missing_binary': dict(latest) if latest else None}
    conn.close()
    with urllib.request.urlopen('http://127.0.0.1:8685/v1/hooks/status', timeout=8) as response:
        status = json.load(response)
    print(json.dumps({'sample_utc': now, 'window_start_utc': begin, 'window_end_utc': end,
                      'window_elapsed': now >= end, 'hub': {key: status['hub'].get(key)
                          for key in ('state', 'pid', 'registry_error', 'journal_error', 'transport_error',
                                      'observation_delivery')},
                      'installed_inventory_status': status['installed_inventory']['status'],
                      'handlers': measured,
                      'interpretation': 'Do not certify a quiet handler; synthetic BB-37 fixture traffic is not normal load. '
                                        'Receipt retention may prune older rows before the window ends; '
                                        'retain intermediate samples if checking the complete 24 hours.'}, indent=2))


if __name__ == '__main__':
    main()
