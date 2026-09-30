"""Narrow HTTPS cost ingress, permanent envelope archive and durable projection.

The archive is authoritative for replay beyond JetStream retention. A response
is acknowledged only after an immutable archive commit AND a JetStream PubAck.
The subscriber ACKs only a confirmed, matching D1 persistence receipt.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import asyncio
from datetime import datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent-hooks'))
from core.validate import validate_envelope

SUBJECT = 'bloodbank.evt.billing.cost.observed'
MAX_BODY = 65536


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def validate(event, scope=None):
    if event.get('type') != 'bloodbank.billing.cost.observed':
        raise ValueError('Only cost observations are accepted')
    validate_envelope(event)
    data = event['data']
    if scope and (data['client_id'], data['project_id']) != (scope['client_id'], scope['project_id']):
        raise PermissionError('Credential does not own this project')
    identity = [data[k] for k in ('client_id', 'project_id', 'provider', 'account_id', 'resource_id', 'period_start', 'period_end', 'currency', 'cost_type')]
    if digest(canonical(identity)) != data['cost_key'] or event['ordering_key'] != data['cost_key']:
        raise ValueError('Cost identity mismatch')
    if data['period_start'] >= data['period_end']:
        raise ValueError('Invalid period')
    if datetime.fromisoformat(data['as_of'].replace('Z', '+00:00')) > datetime.fromisoformat(data['observed_at'].replace('Z', '+00:00')):
        raise ValueError('Invalid freshness')
    if data['coverage'] == 'unavailable' and data['amount_micros'] is not None:
        raise ValueError('Unavailable money must be null')
    if data['coverage'] == 'complete' and data['amount_micros'] is None:
        raise ValueError('Complete coverage requires known money')
    source = data['source']
    if 'evidence' in source and digest(source['evidence']) != source['evidence_sha256']:
        raise ValueError('Source evidence mismatch')
    return event


class Archive:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS envelopes (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, period TEXT NOT NULL,
                  cost_key TEXT NOT NULL, revision INTEGER NOT NULL,
                  body TEXT NOT NULL, sha256 TEXT NOT NULL,
                  UNIQUE(project_id,cost_key,revision));
                CREATE TABLE IF NOT EXISTS delivery (
                  id TEXT PRIMARY KEY REFERENCES envelopes(id), published INTEGER NOT NULL DEFAULT 0,
                  projected INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS quarantine (
                  sha256 TEXT PRIMARY KEY, body BLOB NOT NULL, error TEXT NOT NULL,
                  metadata TEXT NOT NULL, received_at REAL NOT NULL, disposition TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS quarantine_delivery (
                  sha256 TEXT PRIMARY KEY REFERENCES quarantine(sha256),
                  terminated INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
                CREATE TRIGGER IF NOT EXISTS immutable_quarantine_update BEFORE UPDATE ON quarantine
                  BEGIN SELECT RAISE(ABORT,'immutable invalid message evidence'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_quarantine_delete BEFORE DELETE ON quarantine
                  BEGIN SELECT RAISE(ABORT,'immutable invalid message evidence'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_envelope_update BEFORE UPDATE ON envelopes
                  BEGIN SELECT RAISE(ABORT,'immutable cost envelope'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_envelope_delete BEFORE DELETE ON envelopes
                  BEGIN SELECT RAISE(ABORT,'immutable cost envelope'); END;''')
            columns = {r[1] for r in db.execute('PRAGMA table_info(delivery)')}
            if 'received_at' not in columns:
                db.execute('ALTER TABLE delivery ADD COLUMN received_at REAL NOT NULL DEFAULT 0')
            # Previously recorded terminal history stays handled. A new pending
            # disposition already has its own row and survives process restart.
            db.execute('INSERT OR IGNORE INTO quarantine_delivery(sha256,terminated) SELECT sha256,1 FROM quarantine')

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

    def save(self, event):
        body = canonical(event)
        data = event['data']
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT sha256 FROM envelopes WHERE id=?', (event['id'],)).fetchone()
            if prior:
                if prior[0] != digest(body):
                    raise ValueError('Conflicting event identity')
                return
            try:
                db.execute('INSERT INTO envelopes VALUES(?,?,?,?,?,?,?)', (event['id'], data['project_id'], data['period_start'][:7], data['cost_key'], data['revision'], body, digest(body)))
            except sqlite3.IntegrityError as exc:
                raise ValueError('Conflicting cost revision') from exc
            db.execute('INSERT INTO delivery(id,received_at) VALUES(?,?)', (event['id'], time.time()))

    def mark(self, identifier, *, published=None, projected=None, error=None):
        values = {k: v for k, v in locals().items() if k in {'published', 'projected', 'error'} and v is not None}
        with self.connect() as db:
            db.execute('UPDATE delivery SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), identifier))

    def events(self, project=None, month=None, pending=False):
        query = 'SELECT e.body FROM envelopes e JOIN delivery d ON d.id=e.id WHERE 1=1'
        args = []
        for clause, value in [('e.project_id=?', project), ('e.period=?', month)]:
            if value:
                query += ' AND ' + clause
                args.append(value)
        if pending:
            query += ' AND d.published=0'
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(query + ' ORDER BY e.rowid', args)]

    def health(self):
        with self.connect() as db:
            state = dict(zip(('archived', 'pending_publish', 'pending_projection', 'errors'), db.execute("SELECT count(*),coalesce(sum(published=0),0),coalesce(sum(projected=0),0),coalesce(sum(error!=''),0) FROM delivery").fetchone()))
            oldest = db.execute('SELECT min(received_at) FROM delivery WHERE projected=0').fetchone()[0]
            state['oldest_pending_seconds'] = max(0, int(time.time() - oldest)) if oldest is not None else 0
            state['invalid_messages'] = db.execute('SELECT count(*) FROM quarantine').fetchone()[0]
            state['pending_invalid'] = db.execute('SELECT count(*) FROM quarantine_delivery WHERE terminated=0').fetchone()[0]
            state['errors'] += state['pending_invalid']
            return state

    def quarantine(self, message, error):
        body = bytes(message.data)
        metadata = {}
        try:
            observed = message.metadata
            metadata = {'stream': observed.stream, 'consumer': observed.consumer,
                        'stream_sequence': observed.sequence.stream, 'consumer_sequence': observed.sequence.consumer}
        except (AttributeError, ValueError):
            pass
        with self.connect() as db:
            identifier = hashlib.sha256(body).hexdigest()
            db.execute('INSERT OR IGNORE INTO quarantine VALUES(?,?,?,?,?,?)',
                       (identifier, body, type(error).__name__, canonical(metadata), time.time(), 'terminal'))
            db.execute('INSERT OR IGNORE INTO quarantine_delivery(sha256) VALUES(?)', (identifier,))
        return identifier

    def mark_quarantine(self, identifier, *, terminated=0, error=''):
        with self.connect() as db:
            db.execute('UPDATE quarantine_delivery SET terminated=?,error=? WHERE sha256=?', (terminated, error, identifier))


def scopes_from_env():
    # Multiple clients use the same runtime through a scoped mapping. The
    # single-project form keeps initial provisioning simple and references-only.
    if os.environ.get('COST_SCOPES_JSON'):
        scopes = json.loads(os.environ['COST_SCOPES_JSON'])
    else:
        scopes = [{
            'client_id': os.environ['COST_CLIENT_ID'], 'project_id': os.environ['COST_PROJECT_ID'],
            'ingress_token': os.environ['COST_INGRESS_TOKEN'],
            'portal_token': os.environ['COST_PORTAL_INGEST_TOKEN'],
            'portal_url': os.environ.get('COST_PORTAL_URL', 'https://automaticai.io/api/cost-observations'),
        }]
    if not isinstance(scopes, list) or not scopes:
        raise ValueError('Configure a scoped mapping or all single-project fields')
    seen = set()
    tokens = set()
    portal_tokens = set()
    for scope in scopes:
        if (not scope['client_id'] or not scope['project_id'] or scope['project_id'] in seen
                or min(len(scope['ingress_token']), len(scope['portal_token'])) < 32 or scope['ingress_token'] in tokens
                or scope['portal_token'] in portal_tokens):
            raise ValueError('Duplicate project or invalid scoped credential')
        seen.add(scope['project_id'])
        tokens.add(scope['ingress_token'])
        portal_tokens.add(scope['portal_token'])
        url = urlparse(scope['portal_url'])
        if url.scheme != 'https' or url.username or url.password:
            raise ValueError('Projection requires HTTPS without URL credentials')
    return scopes


def authenticate(header, scopes):
    token = header.removeprefix('Bearer ') if header.startswith('Bearer ') else ''
    matches = [s for s in scopes if len(token) >= 32 and hmac.compare_digest(s['ingress_token'], token)]
    if len(matches) != 1:
        raise PermissionError('Unauthorized')
    return matches[0]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def project(event, scope):
    request = urllib.request.Request(scope['portal_url'], data=canonical(event).encode(), headers={
        'authorization': 'Bearer ' + scope['portal_token'], 'content-type': 'application/json', 'user-agent': 'Bloodbank-Cost-Projector/1',
    })
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        result = json.load(response)
        if response.status != 200 or result.get('success') is not True or result.get('event_id') != event['id']:
            raise ValueError('Projection did not confirm this event')


class Bridge:
    def __init__(self, archive, scopes, js, broker_connected=lambda: True):
        self.archive, self.scopes, self.js = archive, scopes, js
        self.broker_connected = broker_connected

    def health(self):
        state = self.archive.health()
        state['broker_connected'] = bool(self.broker_connected())
        state['ready'] = state['broker_connected'] and not state['errors'] and state['oldest_pending_seconds'] <= 120
        return state

    async def publish(self, event):
        self.archive.save(event)
        try:
            receipt = await self.js.publish(SUBJECT, canonical(event).encode(), headers={'Nats-Msg-Id': event['id']}, timeout=10)
            if receipt.stream != 'BLOODBANK_EVENTS':
                raise ValueError('Wrong durable stream')
        except Exception as exc:
            self.archive.mark(event['id'], error='publish:' + type(exc).__name__)
            raise
        self.archive.mark(event['id'], published=1, error='')
        return {'success': True, 'event_id': event['id'], 'stream': receipt.stream, 'sequence': receipt.seq}

    async def consume(self, message):
        try:
            if len(message.data) > MAX_BODY:
                raise ValueError('Cost envelope exceeds the ingestion limit')
            event = validate(json.loads(message.data))
            scope = next(s for s in self.scopes if (s['client_id'], s['project_id']) == (event['data']['client_id'], event['data']['project_id']))
            self.archive.save(event)
        except (ValueError, KeyError, TypeError, AttributeError, PermissionError, StopIteration) as error:
            # Invalid/conflicting/unconfigured envelopes cannot become valid by
            # redelivery. Persist private evidence before terminal disposition.
            identifier = self.archive.quarantine(message, error)
            try:
                await message.term()
            except Exception as terminal_error:
                self.archive.mark_quarantine(identifier, error=type(terminal_error).__name__)
                raise
            self.archive.mark_quarantine(identifier, terminated=1)
            return
        except Exception:
            await message.nak(delay=60)
            return
        try:
            await asyncio.to_thread(project, event, scope)
            self.archive.mark(event['id'], published=1, projected=1, error='')
            await message.ack_sync(timeout=10)
        except Exception as error:
            # Keep only error class, never vendor response or credential-bearing URLs.
            self.archive.mark(event['id'], error=type(error).__name__)
            print(json.dumps({'projection': 'retry', 'error': type(error).__name__}), flush=True)
            await message.nak(delay=60)

    async def consume_next(self, subscription):
        # The projection can consume its full 30-second HTTP deadline. Fetching
        # a sequential batch starts every ACK timer before its work can begin.
        for message in await subscription.fetch(1, timeout=2):
            await self.consume(message)


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

        def do_GET(self):
            if self.path != '/healthz':
                return self.respond(404, {'error': 'Not found'})
            state = bridge.health()
            return self.respond(200 if state['ready'] else 503, state)

        def do_POST(self):
            if self.path != '/v1/cost-observations':
                return self.respond(404, {'error': 'Not found'})
            try:
                scope = authenticate(self.headers.get('Authorization', ''), bridge.scopes)
            except PermissionError:
                return self.respond(401, {'error': 'Unauthorized'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 1 or size > MAX_BODY:
                    return self.respond(413, {'error': 'Invalid body size'})
                self.connection.settimeout(15)
                event = validate(json.loads(self.rfile.read(size)), scope)
                bridge.archive.save(event)
            except PermissionError:
                return self.respond(403, {'error': 'Project scope refused'})
            except Exception:
                return self.respond(409, {'error': 'Invalid or conflicting cost event'})
            try:
                result = asyncio.run_coroutine_threadsafe(bridge.publish(event), loop).result(timeout=20)
                return self.respond(200, result)
            except Exception:
                return self.respond(503, {'error': 'Durable publish pending; retry the same event'})
    return Handler


async def run(args):
    import nats
    from nats.js.api import ConsumerConfig, AckPolicy, DeliverPolicy
    scopes = scopes_from_env()
    archive = Archive(os.environ.get('COST_ARCHIVE_PATH', '/data/cost-envelope-archive.sqlite'))
    nc = await nats.connect(os.environ.get('NATS_URL', 'nats://bloodbank-nats:4222'), name='cost-bridge', max_reconnect_attempts=-1)
    bridge = Bridge(archive, scopes, nc.jetstream(), broker_connected=lambda: nc.is_connected)
    if args.command == 'replay':
        count = 0
        for event in archive.events(args.project, args.month):
            await bridge.publish(event)
            count += 1
        print(json.dumps({'replayed': count, 'original_ids': True}))
        await nc.drain()
        return
    sub = await bridge.js.pull_subscribe(SUBJECT, durable='portal-cost-observations-v1', stream='BLOODBANK_EVENTS', config=ConsumerConfig(ack_policy=AckPolicy.EXPLICIT, deliver_policy=DeliverPolicy.ALL, ack_wait=90, max_ack_pending=32))
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), handler(bridge, asyncio.get_running_loop()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        for event in archive.events(pending=True):
            try:
                await bridge.publish(event)
            except Exception:
                break
        try:
            await bridge.consume_next(sub)
        except (asyncio.TimeoutError, nats.errors.TimeoutError):
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['serve', 'replay'], nargs='?', default='serve')
    parser.add_argument('--project')
    parser.add_argument('--month')
    asyncio.run(run(parser.parse_args()))
