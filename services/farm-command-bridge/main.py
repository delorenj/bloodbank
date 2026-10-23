"""Narrow HTTPS ingress for the Farm's Create Invoice command (Milkshake Story 4.5).

The portal Worker POSTs a signed request to /v1/invoice-commands. This bridge checks the
HMAC-SHA256 over the raw bytes before it parses anything, then the canonical form, the request
fields, the five-minute window, archives the request immutably (SQLite, synchronous=FULL), wraps
it in a bloodbank.billing.invoice.generate command envelope and answers 202 only after the
JetStream PubAck on BLOODBANK_COMMANDS. Milkshake's milkshake-farm-consumer re-verifies the
signature and decides on its own ledger who may ask for what; this bridge authorizes nothing.

Contract: Milkshake docs/farm-commands.md sections 2, 4 and 5. Precedent: services/cost-bridge.
Logs name command_id, status and code only: never a body, reason, key or signature.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent-hooks'))
from core.validate import validate_envelope  # noqa: E402

TYPE = 'bloodbank.billing.invoice.generate'
SUBJECT = 'bloodbank.cmd.billing.invoice.generate'
STREAM = 'BLOODBANK_COMMANDS'
PATH = '/v1/invoice-commands'
MAX_BODY = 4096
WINDOW = timedelta(seconds=300)
MIN_KEY_LENGTH = 32
NS_FARM_COMMAND = uuid.UUID('50c023df-51df-574c-be02-94deb1cbdfd5')
FIELDS = ('schema_version', 'command_id', 'client_request_id', 'client', 'period', 'date_from',
          'date_to', 'mode', 'accept_incomplete_reason', 'approved_by', 'requested_by',
          'session_id', 'issued_at')
SIGNATURE = re.compile(r'sha256=([0-9a-f]{64})\Z')
INSTANT = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z')


class Refusal(Exception):
    """An HTTP refusal with a stable code (contract section 10)."""

    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


def canonical(value):
    """Milkshake's canonical(): sorted keys (code-point order), no whitespace, raw UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha256_hex(raw):
    return hashlib.sha256(raw).hexdigest()


def sign(key, body):
    return hmac.new(key.encode('utf-8'), body, hashlib.sha256).hexdigest()


def command_id_for(session_id, client_request_id):
    return str(uuid.uuid5(NS_FARM_COMMAND, f'{session_id}:{client_request_id}'))


def stamp(moment):
    return moment.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def keys_from_env(text):
    """FARM_COMMAND_KEYS: {"<key_id>": "<key>", ...}; the current key and, mid-rotation, the old."""
    keys = json.loads(text)
    if not isinstance(keys, dict) or not keys:
        raise ValueError('FARM_COMMAND_KEYS holds no keys')
    for key_id, key in keys.items():
        if not isinstance(key, str) or len(key) < MIN_KEY_LENGTH or not key_id:
            raise ValueError(f'FARM_COMMAND_KEYS: key {key_id!r} is unusable')
    return keys


def verify(raw, key_id, signature_header, keys):
    """Step 2: a known key and a matching HMAC over the raw bytes. Nothing is parsed before it."""
    match = SIGNATURE.match(signature_header or '')
    if not match:
        raise Refusal(401, 'SIGNATURE_MISSING')
    key = keys.get(key_id or '')
    if key is None:
        raise Refusal(401, 'KEY_UNKNOWN')
    if not hmac.compare_digest(sign(key, raw), match.group(1)):
        raise Refusal(401, 'SIGNATURE_INVALID')
    return match.group(1)


def _no_duplicates(pairs):
    seen = {}
    for name, value in pairs:
        if name in seen:
            raise ValueError('duplicate key')
        seen[name] = value
    return seen


def parse(raw):
    """Step 3: UTF-8 JSON whose canonical form is exactly the bytes that were signed."""
    try:
        request = json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refusal(400, 'BODY_INVALID') from None
    if not isinstance(request, dict):
        raise Refusal(400, 'BODY_INVALID')
    if canonical(request).encode('utf-8') != raw:
        raise Refusal(422, 'BODY_NOT_CANONICAL')
    return request


def check_request(request):
    """Step 4: the rules of contract section 2.1 the JSON Schema cannot say (the schema runs
    on the envelope at step 7 and covers each field's type and pattern)."""
    def bad():
        return Refusal(422, 'REQUEST_INVALID')
    if set(request) != set(FIELDS) or request['schema_version'] != 1 or isinstance(request['schema_version'], bool):
        raise bad()
    text = ('command_id', 'client_request_id', 'client', 'mode', 'requested_by', 'session_id', 'issued_at')
    if not all(isinstance(request[f], str) for f in text):
        raise bad()
    if request['command_id'] != command_id_for(request['session_id'], request['client_request_id']):
        raise bad()
    if request['mode'] not in ('plan', 'apply') or not INSTANT.match(request['issued_at']):
        raise bad()
    period, start, end = request['period'], request['date_from'], request['date_to']
    try:
        if period is not None:
            if start is not None or end is not None:
                raise bad()
        elif start is None or end is None or date.fromisoformat(end) < date.fromisoformat(start):
            raise bad()
    except (TypeError, ValueError):
        raise bad() from None
    reason, approver = request['accept_incomplete_reason'], request['approved_by']
    if reason is not None:
        if request['mode'] != 'apply' or approver != request['requested_by'] or not isinstance(reason, str) or reason != reason.strip():
            raise bad()
    elif approver is not None:
        raise bad()
    if request['requested_by'] != request['requested_by'].strip().lower():
        raise bad()
    return request


def issued_at(request):
    return datetime.strptime(request['issued_at'], '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=UTC)


def check_fresh(request, now):
    """Step 5: signed within five minutes of this clock, either direction."""
    if abs(now - issued_at(request)) > WINDOW:
        raise Refusal(422, 'COMMAND_EXPIRED')


def envelope(request, key_id, signature, body_sha256, received_at):
    """The command envelope (contract section 5). The Worker never sees it."""
    command_id = request['command_id']
    return {
        'specversion': '1.0',
        'id': command_id,
        'source': 'urn:33god:service:farm-command-bridge',
        'type': TYPE,
        'subject': SUBJECT,
        'time': received_at,
        'datacontenttype': 'application/json',
        'dataschema': f'apicurio://holyfields/{TYPE}/versions/1',
        'schemaref': f'{TYPE}.v1',
        'kind': 'command',
        'domain': 'billing',
        'producer': 'farm-command-bridge',
        'service': 'farm-command-bridge',
        # The CLAIMED actor: the consumer trusts it only after it re-verifies the signature.
        'actor': {'type': 'operator', 'agent_id': 'farm:' + request['requested_by']},
        'correlationid': command_id,
        'causationid': None,
        'command_id': command_id,
        'idempotency_key': f"billing.invoice.generate:client:{request['client']}:command:{command_id}",
        'delivery': 'single_consumer',
        'data': {
            'schema_version': 1,
            'request': request,
            'signature': {'alg': 'hmac-sha256', 'key_id': key_id, 'value': signature},
            'body_sha256': body_sha256,
            'received_at': received_at,
        },
    }


class Archive:
    """command_id -> the signed request and its envelope, immutable; delivery state beside it."""

    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS commands (
                  command_id TEXT PRIMARY KEY, body_sha256 TEXT NOT NULL, body BLOB NOT NULL,
                  envelope TEXT NOT NULL, received_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS delivery (
                  command_id TEXT PRIMARY KEY REFERENCES commands(command_id),
                  published INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
                CREATE TRIGGER IF NOT EXISTS immutable_command_update BEFORE UPDATE ON commands
                  BEGIN SELECT RAISE(ABORT,'immutable farm command'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_command_delete BEFORE DELETE ON commands
                  BEGIN SELECT RAISE(ABORT,'immutable farm command'); END;''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, command_id, raw, event):
        """Insert, or report the prior row: ('new'|'duplicate', envelope, published)."""
        digest = sha256_hex(raw)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT c.body_sha256, c.envelope, d.published FROM commands c JOIN delivery d USING(command_id) WHERE command_id=?', (command_id,)).fetchone()
            if prior:
                if not hmac.compare_digest(prior[0], digest):
                    raise Refusal(409, 'COMMAND_ID_CONFLICT')
                return 'duplicate', json.loads(prior[1]), bool(prior[2])
            db.execute('INSERT INTO commands VALUES(?,?,?,?,?)', (command_id, digest, raw, canonical(event), event['time']))
            db.execute('INSERT INTO delivery(command_id) VALUES(?)', (command_id,))
            return 'new', event, False

    def mark(self, command_id, *, published=None, error=None):
        values = {k: v for k, v in (('published', published), ('error', error)) if v is not None}
        with self.connect() as db:
            db.execute('UPDATE delivery SET ' + ','.join(k + '=?' for k in values) + ' WHERE command_id=?', (*values.values(), command_id))

    def pending(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT c.envelope FROM commands c JOIN delivery d USING(command_id) WHERE d.published=0 ORDER BY c.rowid')]

    def get(self, command_id):
        with self.connect() as db:
            row = db.execute('SELECT envelope FROM commands WHERE command_id=?', (command_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def health(self):
        with self.connect() as db:
            archived, unpublished, errors = db.execute("SELECT count(*), coalesce(sum(published=0),0), coalesce(sum(error!=''),0) FROM delivery").fetchone()
            last = db.execute("SELECT error FROM delivery WHERE error!='' ORDER BY rowid DESC LIMIT 1").fetchone()
        return {'archived': archived, 'unpublished': unpublished, 'errors': errors, 'last_error': last[0] if last else None}


def log(**fields):
    print(canonical(fields), flush=True)


class Bridge:
    def __init__(self, archive, keys, js, broker_connected=lambda: True, clock=lambda: datetime.now(UTC)):
        self.archive, self.keys, self.js = archive, keys, js
        self.broker_connected, self.clock = broker_connected, clock

    def health(self):
        """Liveness is answering at all; readiness (broker up, archive drained) is in the body.

        The container healthcheck reads only liveness, so Traefik keeps routing through a NATS
        blip: a POST then answers 503 BUS_UNAVAILABLE with the command archived, and the sweep
        publishes it once the broker is back.
        """
        try:
            state = self.archive.health()
        except Exception as exc:
            state = {'archived': None, 'unpublished': None, 'errors': None,
                     'last_error': 'archive:' + type(exc).__name__}
        state['live'] = True
        state['broker_connected'] = bool(self.broker_connected())
        state['ready'] = state['broker_connected'] and state['unpublished'] == 0
        return state

    async def publish(self, event):
        command_id = event['id']
        try:
            receipt = await self.js.publish(SUBJECT, canonical(event).encode('utf-8'), headers={'Nats-Msg-Id': command_id}, timeout=10)
            if receipt.stream != STREAM:
                raise ValueError('Wrong durable stream')
        except Exception as exc:
            self.archive.mark(command_id, error='publish:' + type(exc).__name__)
            raise
        self.archive.mark(command_id, published=1, error='')
        return receipt

    def accept(self, raw, key_id, signature_header):
        """Steps 1 to 6, synchronous: (status, event, published). Raises Refusal."""
        if len(raw) > MAX_BODY:
            raise Refusal(413, 'BODY_TOO_LARGE')
        signature = verify(raw, key_id, signature_header, self.keys)
        request = check_request(parse(raw))
        now = self.clock()
        check_fresh(request, now)
        event = envelope(request, key_id, signature, sha256_hex(raw), stamp(now))
        try:
            validate_envelope(event)
        except Exception:
            raise Refusal(422, 'REQUEST_INVALID') from None
        return self.archive.save(request['command_id'], raw, event)

    async def sweep(self):
        """Publish every archived, unpublished command; one that fails never holds up the rest.

        Skipped while the broker is down (every publish would wait out its timeout). A row that
        cannot publish is logged and marked with its error, and the sweep moves on to the next.
        """
        if not self.broker_connected():
            return
        try:
            pending = list(self.archive.pending())
        except Exception as exc:
            log(status='archive_pending_failed', code=type(exc).__name__)
            await asyncio.sleep(15)
            return
        for event in pending:
            try:
                await self.publish(event)
                log(command_id=event['id'], status='republished')
            except Exception as exc:
                log(command_id=event['id'], status='republish_failed', code=type(exc).__name__)


def handler(bridge, loop):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, code, value):
            body = canonical(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def refuse(self, refusal, command_id=None):
            log(command_id=command_id, status=refusal.status, code=refusal.code)
            return self.respond(refusal.status, {'success': False, 'error': refusal.code, 'code': refusal.code})

        def do_GET(self):
            # /healthz is liveness (always 200 while the process serves; readiness in the body),
            # which is what the container healthcheck and so Traefik's routing read. /readyz is
            # the same body with 503 while not ready, for monitors that alert on a status code.
            if self.path not in ('/healthz', '/readyz'):
                return self.respond(404, {'error': 'Not found'})
            state = bridge.health()
            ready_code = 200 if state['ready'] else 503
            return self.respond(200 if self.path == '/healthz' else ready_code, state)

        def do_POST(self):
            if self.path != PATH:
                return self.respond(404, {'error': 'Not found'})
            try:
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise Refusal(400, 'BODY_INVALID') from None
                if size > MAX_BODY:
                    raise Refusal(413, 'BODY_TOO_LARGE')
                if size < 1 or self.headers.get_content_type() != 'application/json':
                    raise Refusal(400, 'BODY_INVALID')
                self.connection.settimeout(15)
                raw = self.rfile.read(size)
                status, event, published = bridge.accept(raw, self.headers.get('X-Milkshake-Key-Id'), self.headers.get('X-Milkshake-Signature'))
            except Refusal as refusal:
                return self.refuse(refusal)
            command_id = event['id']
            if not published:
                try:
                    asyncio.run_coroutine_threadsafe(bridge.publish(event), loop).result(timeout=20)
                except Exception:
                    return self.refuse(Refusal(503, 'BUS_UNAVAILABLE'), command_id)
            duplicate = status == 'duplicate'
            log(command_id=command_id, status=200 if duplicate else 202, code='DUPLICATE' if duplicate else 'ACCEPTED')
            return self.respond(200 if duplicate else 202, {
                'success': True, 'duplicate': duplicate, 'command_id': command_id, 'received_at': event['time'],
            })
    return Handler


async def run(args):
    import nats
    keys = keys_from_env(os.environ['FARM_COMMAND_KEYS'])
    archive = Archive(os.environ.get('FARM_COMMAND_ARCHIVE_PATH', '/data/farm-command-archive.sqlite'))
    nc = await nats.connect(os.environ.get('NATS_URL', 'nats://bloodbank-nats:4222'), name='farm-command-bridge', max_reconnect_attempts=-1)
    bridge = Bridge(archive, keys, nc.jetstream(), broker_connected=lambda: nc.is_connected)
    if args.command == 'republish':
        event = archive.get(args.command_id)
        if event is None:
            raise SystemExit('unknown command_id')
        receipt = await bridge.publish(event)
        print(canonical({'command_id': event['id'], 'stream': receipt.stream, 'sequence': receipt.seq, 'duplicate': bool(getattr(receipt, 'duplicate', False))}))
        await nc.drain()
        return
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), handler(bridge, asyncio.get_running_loop()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(status='listening', key_ids=sorted(keys))
    while True:
        await bridge.sweep()
        await asyncio.sleep(15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['serve', 'republish'], nargs='?', default='serve')
    parser.add_argument('--command-id')
    asyncio.run(run(parser.parse_args()))
